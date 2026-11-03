"""Restricted aggregate SQL shapes over aggregate-only columns.

Not a privacy proof for arbitrary BigQuery: a query that reads demographic
columns must be one flat aggregate SELECT over buyers and their purchases, so
the service can add a contributing-buyer threshold (see scope.add_cohort_guard).
"""

from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from retail_agent.schema.catalog import TABLE_POLICIES
from retail_agent.schema.models import DataAccessLevel

COHORT_COLUMN = "__cohort_size"

# Allowed source sets: buyers with their purchases, optionally the products.
_SOURCE_SETS = ({"users", "order_items"}, {"users", "order_items", "products"})
# The only joins: a purchase to its buyer and to its product.
_JOIN_KEYS = {
    frozenset({("users", "id"), ("order_items", "user_id")}),
    frozenset({("products", "id"), ("order_items", "product_id")}),
}
# order_items columns usable for filters and grouping (not order/item identifiers).
_ORDER_ITEM_DIMENSIONS = {
    "product_id",
    "status",
    "sale_price",
    "created_at",
    "shipped_at",
    "delivered_at",
    "returned_at",
}
_AGGREGATES = (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)
# Columns that numeric aggregates (SUM/AVG/MIN/MAX) may read.
_NUMERIC_INPUTS = {("users", "age"), ("order_items", "sale_price"), ("products", "cost"), ("products", "retail_price")}
# Expression nodes allowed inside a numeric aggregate: plain arithmetic only.
_METRIC_NODES = {
    exp.Column,
    exp.Identifier,
    exp.Literal,
    exp.Null,
    exp.Paren,
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    exp.Neg,
    exp.Round,
    exp.Abs,
    exp.Cast,
    exp.DataType,
    exp.DataTypeParam,
    exp.SafeDivide,
    exp.Nullif,
}


class DemographicQuery:
    """Checks one qualified SELECT; `validate()` returns the users alias (the cohort subject).

    Rules run in a fixed order and raise ValueError with a model-actionable message.
    """

    def __init__(self, tree):
        self.tree = tree
        self.sources: dict = {}
        self.groups: list = []
        self.aggregates: list = []

    def validate(self) -> str:
        self._check_flat_select()
        self._check_joins()
        self._check_filters_and_grouping()
        self._check_outputs()
        return next(alias for alias, source in self.sources.items() if source.name == "users")

    def _check_flat_select(self) -> None:
        tree = self.tree
        scopes = list(traverse_scope(tree))
        if (
            len(scopes) != 1
            or not isinstance(tree, exp.Select)
            or tree.args.get("with_")
            or any(tree.find_all(exp.Window))
        ):
            raise ValueError(
                "Demographic SQL requires one flat aggregate SELECT: no CTEs, subqueries, set operations or windows."
            )
        self.sources = scopes[0].sources
        if any(not isinstance(source, exp.Table) for source in self.sources.values()):
            raise ValueError("Demographic SQL requires ordinary physical sources.")
        names = [source.name for source in self.sources.values()]
        if len(names) != len(set(names)) or set(names) not in _SOURCE_SETS:
            raise ValueError("Demographic SQL must join users to order_items, optionally products, exactly once.")

    def _check_joins(self) -> None:
        links = set()
        for join in self.tree.args.get("joins") or []:
            on = join.args.get("on")
            if (
                join.side
                or join.kind not in {"", "INNER"}
                or not isinstance(on, exp.EQ)
                or not all(isinstance(column, exp.Column) for column in (on.left, on.right))
            ):
                raise ValueError("Use INNER JOINs with canonical key equality; put filters in WHERE.")
            pair = frozenset((self._key(on.left), self._key(on.right)))
            if pair not in _JOIN_KEYS or pair in links:
                raise ValueError("Demographic joins must link purchases to their actual buyers/products.")
            links.add(pair)
        if len(links) != len(self.sources) - 1 or self.tree.args.get("distinct"):
            raise ValueError("Use connected canonical joins and GROUP BY, not SELECT DISTINCT.")

    def _check_filters_and_grouping(self) -> None:
        group = self.tree.args.get("group")
        self.groups = group.expressions if group else []
        if group and any(value for name, value in group.args.items() if name != "expressions"):
            raise ValueError("Demographic grouping sets/rollups are not supported.")
        where = self.tree.args.get("where")
        for expression in [*self.groups, *([where] if where else [])]:
            if any(expression.find_all(exp.AggFunc)) or any(
                not self._is_dimension(c) for c in expression.find_all(exp.Column)
            ):
                raise ValueError(
                    "Demographic filters/grouping cannot select individual customer/order/item identifiers."
                )

    def _check_outputs(self) -> None:
        for item in self.tree.expressions:
            self._check_output(item.this if isinstance(item, exp.Alias) else item)
        for name in ("having", "order"):
            if self.tree.args.get(name):
                self._check_output(self.tree.args[name])
        if not self.aggregates:
            raise ValueError("Demographic SQL must return aggregate statistics.")

    def _check_output(self, expression) -> None:
        """An output may combine grouped expressions and checked aggregates only."""
        remaining = expression.copy().transform(self._replace_checked)
        for column in remaining.find_all(exp.Column):
            # ORDER BY may retain an output alias after SQLGlot qualification.
            if column.table or column.name not in self.tree.named_selects:
                raise ValueError(
                    "Demographic outputs must be grouped expressions or aggregates, never individual profiles."
                )

    def _replace_checked(self, node):
        """Replace grouped expressions and valid aggregates with a constant; leave the rest."""
        if any(node == group for group in self.groups):
            return exp.Literal.number(0)
        if isinstance(node, exp.AggFunc):
            self._check_aggregate(node)
            self.aggregates.append(node)
            return exp.Literal.number(0)
        return node

    def _check_aggregate(self, node) -> None:
        if not isinstance(node, _AGGREGATES):
            raise ValueError("Demographic aggregates support COUNT, SUM, AVG, MIN and MAX.")
        argument = node.this
        if isinstance(argument, exp.Distinct):
            if not isinstance(node, exp.Count) or len(argument.expressions) != 1:
                raise ValueError("Only COUNT supports DISTINCT with one direct column.")
            argument = argument.expressions[0]
        if isinstance(node, exp.Count):
            if not isinstance(argument, (exp.Star, exp.Column)):
                raise ValueError("Use COUNT(*) or COUNT([DISTINCT] column), not conditional counts.")
            return
        if argument is None or any(type(n) not in _METRIC_NODES for n in argument.walk()):
            raise ValueError(
                "Use numeric arithmetic inside demographic aggregates; conditional/nested aggregates are not supported."
            )
        if any(self._key(column) not in _NUMERIC_INPUTS for column in argument.find_all(exp.Column)):
            raise ValueError("Numeric demographic aggregates support age, sale price and product cost/retail price.")

    def _key(self, column) -> tuple[str, str]:
        source = self.sources.get(column.table)
        if source is None:
            raise ValueError("Unresolved demographic column source.")
        return source.name, column.name

    def _is_dimension(self, column) -> bool:
        """Filters and groups may use buyer demographics, product attributes and purchase facts."""
        table, name = self._key(column)
        if table == "users":
            return TABLE_POLICIES[table][name].access_level == DataAccessLevel.AGGREGATE_ONLY
        return table == "products" or (table == "order_items" and name in _ORDER_ITEM_DIMENSIONS)
