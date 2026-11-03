"""Whether model-written SQL may run: one read-only SELECT over allowed sources and columns.

SQLGlot resolves CTE/subquery/window scopes; we do not maintain a grammar
allowlist. Unknown/UDF/external functions and source modifiers still fail closed.
Demographic queries are checked structurally, not by a catalog of business tasks.
Rules run in a fixed order; the first failing rule's message is returned.
"""

from sqlglot import exp, parse
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from retail_agent.schema.catalog import CATALOG_NAME, DB_NAME, TABLE_POLICIES
from retail_agent.schema.models import DataAccessLevel
from retail_agent.sql.demographics import COHORT_COLUMN, DemographicQuery


class SQLValidationError(ValueError):
    """Controlled messages only; do not echo SQL or library exception text."""


_IDENTITY_FUNCTIONS = {"SESSION_USER", "CURRENT_USER", "USER", "SYSTEM_USER", "EXTERNAL_QUERY"}


def _is_wildcard_projection(node) -> bool:
    """Any `*` except the argument of a plain COUNT(*)."""
    return isinstance(node, exp.Star) and not (
        isinstance(node.parent, exp.Count) and node.parent.this is node and not any(node.args.values())
    )


# Checked for every syntax node, in this order.
_NODE_RULES = (
    (
        lambda node: isinstance(node, (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Parameter, exp.Placeholder)),
        "Only read-only SQL without custom parameters is allowed",
    ),
    (
        lambda node: isinstance(node, (exp.Anonymous, exp.Dot)),
        "Unknown, remote or user-defined functions are not allowed",
    ),
    (
        lambda node: isinstance(node, exp.Func) and node.sql_name() in _IDENTITY_FUNCTIONS,
        "Identity and external-data functions are not allowed",
    ),
    (_is_wildcard_projection, "Use explicit columns, not wildcard projections"),
)

# Checked for every physical table source, in this order.
_TABLE_RULES = (
    (
        lambda table: table.catalog != CATALOG_NAME or table.db != DB_NAME or table.name not in TABLE_POLICIES,
        "Table is outside the allowed sources",
    ),
    (
        lambda table: not all(isinstance(table.args.get(key), exp.Identifier) for key in ("this", "db", "catalog")),
        "Only ordinary physical table sources are allowed",
    ),
    (
        lambda table: any(value for key, value in table.args.items() if key not in {"this", "db", "catalog", "alias"}),
        "Table source modifiers are not supported",
    ),
    (
        lambda table: table.args.get("alias") and table.args["alias"].args.get("columns"),
        "Physical table column-renaming aliases are not supported",
    ),
)

# Every column of the allowed tables, so SQLGlot can resolve and qualify references.
_QUALIFY_SCHEMA = {
    CATALOG_NAME: {
        DB_NAME: {table: {column: "UNKNOWN" for column in policies} for table, policies in TABLE_POLICIES.items()}
    }
}


def physical_tables(tree):
    """CTE names are Scope references, not physical BigQuery sources."""
    seen = set()
    for scope in traverse_scope(tree):
        for _, source in scope.selected_sources.values():
            if isinstance(source, exp.Table) and id(source) not in seen:
                seen.add(id(source))
                yield source


def validate_query(sql: str):
    """Return a qualified AST ready for brand scoping, or raise SQLValidationError.

    For a demographic query, tree.meta["demographic_subject"] holds the users alias.
    """
    try:
        tree = _single_select(sql)
        _check_each(tree.walk(), _NODE_RULES)
        _check_each(physical_tables(tree), _TABLE_RULES)
        tree = qualify(tree, dialect="bigquery", schema=_QUALIFY_SCHEMA, expand_stars=False)
        _check_output_names(tree)
        if _reads_aggregate_only_columns(tree):
            try:
                tree.meta["demographic_subject"] = DemographicQuery(tree).validate()
            except ValueError as error:
                raise SQLValidationError(str(error)) from None
        return tree
    except (ParseError, OptimizeError):
        raise SQLValidationError("SQL cannot be parsed or its columns are unknown/ambiguous") from None


def _single_select(sql: str):
    statements = [node for node in parse(sql, read="bigquery") if node is not None]
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.SetOperation)):
        raise SQLValidationError("Expected one read-only SELECT query")
    return statements[0]


def _check_each(items, rules) -> None:
    for item in items:
        for broken, message in rules:
            if broken(item):
                raise SQLValidationError(message)


def _check_output_names(tree) -> None:
    if any(
        item.alias_or_name.lower() == COHORT_COLUMN
        for select in tree.find_all(exp.Select)
        for item in select.expressions
    ):
        raise SQLValidationError("The cohort output name is reserved for trusted code")
    if len(tree.named_selects) != len(set(tree.named_selects)):
        raise SQLValidationError("Use unique output column aliases")


def _reads_aggregate_only_columns(tree) -> bool:
    """Check every column's access level; True if any is aggregate-only (demographic)."""
    demographic = False
    for scope in traverse_scope(tree):
        for column in scope.columns:
            source = _column_source(scope, column)
            if not isinstance(source, exp.Table):
                continue
            policy = TABLE_POLICIES[source.name].get(column.name)
            if policy is None:
                raise SQLValidationError("Unknown column")
            if policy.access_level == DataAccessLevel.BLOCKED_PII:
                raise SQLValidationError("PII column is forbidden")
            if policy.access_level == DataAccessLevel.BLOCKED_UNSCOPED:
                raise SQLValidationError(policy.description)
            demographic = demographic or policy.access_level == DataAccessLevel.AGGREGATE_ONLY
    return demographic


def _column_source(scope, column):
    owner = scope
    while owner is not None and column.table not in owner.sources:
        owner = owner.parent  # Correlated references resolve outside.
    source = owner.sources.get(column.table) if owner is not None else None
    if source is None:
        raise SQLValidationError("Column source is unresolved or ambiguous")
    return source
