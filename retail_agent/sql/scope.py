"""Rewrite a validated query so every physical source is limited to trusted brands.

No queries are executed here. QueryService MUST bind allowed_brands as a
BigQuery ARRAY<STRING> parameter from trusted identity/entitlements, never from
model output. This rewrite is not a substitute for database permissions, cost
limits, aggregate protection, or result validation.
"""

from sqlglot import exp, parse_one

from retail_agent.schema.catalog import CATALOG_NAME, DB_NAME, TABLE_POLICIES
from retail_agent.schema.models import DataAccessLevel
from retail_agent.sql.demographics import COHORT_COLUMN
from retail_agent.sql.validator import physical_tables, validate_query


def _table_path(name: str) -> str:
    # All names here come from application constants, never model output.
    return f"`{CATALOG_NAME}.{DB_NAME}.{name}`"


def _sold_with_allowed_brand(relation: str) -> str:
    """Rows linked (by `relation`) to at least one order item of an allowed brand."""
    return f"""EXISTS (
            SELECT 1 FROM {_table_path("order_items")} AS oi
            JOIN {_table_path("products")} AS p ON p.id = oi.product_id
            WHERE {relation}
              AND p.brand IN UNNEST(@allowed_brands)
        )"""


# How each table's row `r` is tied to the allowed brands.
_BRAND_PREDICATES = {
    "products": "r.brand IN UNNEST(@allowed_brands)",
    "order_items": f"""EXISTS (
            SELECT 1 FROM {_table_path("products")} AS p
            WHERE p.id = r.product_id
              AND p.brand IN UNNEST(@allowed_brands)
        )""",
    "users": _sold_with_allowed_brand("oi.user_id = r.id"),
    "orders": _sold_with_allowed_brand("oi.order_id = r.order_id"),
}


def _scoped_source(table_name: str, *, demographics: bool = False) -> exp.Select:
    """The table's allowed columns, restricted to rows of the allowed brands."""
    columns = ", ".join(
        f"r.`{name}`"
        for name, policy in TABLE_POLICIES[table_name].items()
        if policy.access_level == DataAccessLevel.ALLOWED
        or demographics
        and policy.access_level == DataAccessLevel.AGGREGATE_ONLY
    )
    return parse_one(
        f"SELECT {columns} FROM {_table_path(table_name)} AS r WHERE {_BRAND_PREDICATES[table_name]}",
        read="bigquery",
    )


def add_cohort_guard(tree, minimum: int):
    """Add a service-owned contributing-buyer guard before ORDER/LIMIT."""
    alias = tree.meta.get("demographic_subject")
    if not alias:
        return
    if type(minimum) is not int or minimum < 2:
        raise ValueError("minimum cohort size must be at least 2")
    inputs = {}
    for aggregate in tree.find_all(exp.AggFunc):
        arg = aggregate.this
        if isinstance(arg, exp.Distinct):
            arg = arg.expressions[0]
        if arg is not None and not isinstance(arg, exp.Star):
            for item in [arg, *arg.find_all(exp.Column)]:
                inputs[item.sql(dialect="bigquery")] = item
    subject = exp.column("id", table=alias)
    if inputs:
        # A buyer counts only if every aggregate input is present for them.
        checks = [exp.Not(this=exp.Is(this=item.copy(), expression=exp.Null())) for item in inputs.values()]
        subject = exp.If(this=exp.and_(*checks), true=subject, false=exp.Null())
    count = exp.Count(this=exp.Distinct(expressions=[subject]))
    tree.select(exp.alias_(count.copy(), COHORT_COLUMN), append=True, copy=False)
    predicate = exp.GTE(this=count, expression=exp.Literal.number(minimum))
    existing = tree.args.get("having")
    tree.set("having", exp.Having(this=exp.and_(existing.this.copy(), predicate) if existing else predicate))


def scope_query(tree, *, minimum_cohort_size: int = 5) -> str:
    """Scope a tree from validate_query (modified in place); returns BigQuery SQL.

    Physical sources inside CTEs and subqueries are replaced too.
    """
    demographics = bool(tree.meta.get("demographic_subject"))
    add_cohort_guard(tree, minimum_cohort_size)
    # Snapshot BEFORE replacement: never recursively rewrite our own guards.
    for table in list(physical_tables(tree)):
        alias = table.args.get("alias")
        table.replace(
            exp.Subquery(
                this=_scoped_source(table.name, demographics=demographics),
                alias=alias.copy()
                if alias is not None
                else exp.TableAlias(this=exp.to_identifier(table.name, quoted=True)),
            )
        )
    return tree.sql(dialect="bigquery", pretty=True)


def apply_brand_scope(sql: str, *, minimum_cohort_size: int = 5) -> str:
    """Validate and scope SQL text (for callers that do not keep the validated tree)."""
    return scope_query(validate_query(sql), minimum_cohort_size=minimum_cohort_size)
