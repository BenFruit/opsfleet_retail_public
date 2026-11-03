"""Offline contract and row-isolation tests; no Google credentials needed."""

import sqlite3
import unittest

from sqlglot import exp, parse_one

from retail_agent.sql.scope import apply_brand_scope
from retail_agent.sql.validator import validate_query

DATASET = "bigquery-public-data.thelook_ecommerce"


class QueryScopeTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        # Include every projected field, without introducing pandas/BQ dependencies.
        from retail_agent.schema.catalog import TABLE_POLICIES

        for table, columns in TABLE_POLICIES.items():
            fields = ", ".join(f'"{name}"' for name in columns)
            self.db.execute(f'CREATE TABLE "{table}" ({fields})')

        self.db.executemany(
            "INSERT INTO products (id, name, brand, cost) VALUES (?, ?, ?, ?)",
            [(1, "A product", "Brand A", 3), (2, "B product", "Brand B", 4), (3, "C product", "Brand C", 5)],
        )
        self.db.executemany("INSERT INTO users (id) VALUES (?)", [(10,), (20,), (30,)])
        self.db.executemany(
            "INSERT INTO orders (order_id, user_id, num_of_item) VALUES (?, ?, ?)",
            [(100, 10, 3), (200, 20, 1)],
        )
        self.db.executemany(
            "INSERT INTO order_items (id, order_id, user_id, product_id, sale_price) VALUES (?, ?, ?, ?, ?)",
            [(1, 100, 10, 1, 10), (2, 100, 10, 2, 20), (3, 100, 10, 3, 900), (4, 200, 20, 3, 100)],
        )

    def execute_scoped(self, sql, brands=("Brand A", "Brand B")):
        """TEST ONLY: adapt generated BQ SQL to our tiny SQLite fixture.

        Production must use BigQuery parameters, NOT this literal substitution.
        The fixture checks row semantics, not BigQuery dialect compatibility.
        """
        tree = parse_one(apply_brand_scope(sql), read="bigquery")
        for predicate in list(tree.find_all(exp.In)):
            if predicate.args.get("unnest") is not None:
                predicate.replace(
                    exp.In(
                        this=predicate.this.copy(),
                        expressions=[exp.Literal.string(brand) for brand in brands],
                    )
                )
        for table in tree.find_all(exp.Table):
            table.set("catalog", None)
            table.set("db", None)
        return self.db.execute(tree.sql(dialect="sqlite")).fetchall()

    def test_products(self):
        self.assertEqual(self.execute_scoped(f"SELECT p.id FROM `{DATASET}.products` AS p ORDER BY p.id"), [(1,), (2,)])

    def test_unqualified_column_is_allowed_when_unambiguous(self):
        self.assertEqual(self.execute_scoped(f"SELECT id FROM `{DATASET}.products` ORDER BY id"), [(1,), (2,)])

    def test_cte_and_derived_sources_are_supported_and_scoped(self):
        for sql in (
            f"WITH x AS (SELECT p.id FROM `{DATASET}.products` p) SELECT x.id FROM x ORDER BY x.id",
            f"SELECT x.id FROM (SELECT p.id FROM `{DATASET}.products` p) x ORDER BY x.id",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(self.execute_scoped(sql), [(1,), (2,)])

    def test_cte_shadowing_a_physical_table_name_is_not_mistaken_for_that_table(self):
        self.assertEqual(
            self.execute_scoped(
                f"WITH users AS (SELECT p.id FROM `{DATASET}.products` p) SELECT users.id FROM users ORDER BY users.id"
            ),
            [(1,), (2,)],
        )

    def test_window_function_only_sees_authorized_rows(self):
        self.assertEqual(
            self.execute_scoped(
                f"SELECT p.id, SUM(p.cost) OVER (ORDER BY p.id) AS running_cost FROM `{DATASET}.products` p ORDER BY p.id"
            ),
            [(1, 3), (2, 7)],
        )

    def test_correlated_reference_is_supported(self):
        self.assertEqual(
            self.execute_scoped(f"""SELECT p.id FROM `{DATASET}.products` p
            WHERE EXISTS (SELECT 1 FROM `{DATASET}.order_items` oi WHERE oi.product_id=p.id)
            ORDER BY p.id"""),
            [(1,), (2,)],
        )

    def test_union_sources_are_independently_scoped(self):
        rows = self.execute_scoped(f"""SELECT p.id FROM `{DATASET}.products` p
            UNION DISTINCT SELECT oi.product_id FROM `{DATASET}.order_items` oi""")
        self.assertEqual(sorted(rows), [(1,), (2,)])

    def test_builtin_functions_do_not_need_a_handwritten_allowlist(self):
        self.assertEqual(
            self.execute_scoped(f"SELECT p.id, ROUND(p.cost, 2) AS rounded FROM `{DATASET}.products` p ORDER BY p.id"),
            [(1, 3.0), (2, 4.0)],
        )

    def test_unqualified_grouping_dimension_resolves_to_its_real_table(self):
        self.assertEqual(
            self.execute_scoped(f"""
            SELECT p.brand, SUM(oi.sale_price) AS revenue,
                   SUM(oi.sale_price - p.cost) AS profit
            FROM `{DATASET}.products` AS p
            JOIN `{DATASET}.order_items` AS oi ON oi.product_id = p.id
            GROUP BY brand ORDER BY profit DESC LIMIT 10
        """),
            [("Brand B", 20, 16), ("Brand A", 10, 7)],
        )

    def test_order_items_without_product_join(self):
        self.assertEqual(self.execute_scoped(f"SELECT SUM(oi.sale_price) FROM `{DATASET}.order_items` AS oi"), [(30,)])

    def test_customers_are_not_duplicated_by_purchases(self):
        self.assertEqual(self.execute_scoped(f"SELECT u.id FROM `{DATASET}.users` AS u"), [(10,)])

    def test_orders_are_not_duplicated_by_items(self):
        self.assertEqual(self.execute_scoped(f"SELECT o.order_id FROM `{DATASET}.orders` AS o"), [(100,)])

    def test_customer_join_does_not_unlock_other_brand_purchases(self):
        self.assertEqual(
            self.execute_scoped(f"""
            SELECT u.id, SUM(oi.sale_price)
            FROM `{DATASET}.users` AS u
            JOIN `{DATASET}.order_items` AS oi ON oi.user_id = u.id
            GROUP BY u.id
        """),
            [(10, 30)],
        )

    def test_other_manager_sees_only_other_brands(self):
        self.assertEqual(
            self.execute_scoped(
                f"SELECT SUM(oi.sale_price) FROM `{DATASET}.order_items` AS oi",
                brands=("Brand C",),
            ),
            [(1000,)],
        )

    def test_empty_brands_do_not_grant_all_rows(self):
        self.assertEqual(
            self.execute_scoped(
                f"SELECT p.id FROM `{DATASET}.products` AS p",
                brands=(),
            ),
            [],
        )

    def test_model_schema_names_only_plannable_columns(self):
        from unittest.mock import Mock

        from retail_agent.llm.analyst import Analyst

        schema = {
            "orders": [{"name": "order_id", "type": "INT64"}, {"name": "num_of_item", "type": "INT64"}],
            "users": [{"name": "email", "type": "STRING"}, {"name": "age", "type": "INT64"}],
        }
        visible = Analyst(Mock(), schema, persona_style=Mock()).schema
        self.assertEqual(
            {t: [c["name"] for c in cols] for t, cols in visible.items()}, {"orders": ["order_id"], "users": ["age"]}
        )

    def test_aliasless_table_reference(self):
        self.assertEqual(
            self.execute_scoped(f"SELECT products.id FROM `{DATASET}.products` ORDER BY products.id"), [(1,), (2,)]
        )

    def test_reused_aliases_in_nested_scopes(self):
        self.assertEqual(
            self.execute_scoped(f"""
            SELECT x.cost FROM `{DATASET}.products` AS x
            WHERE x.id IN (
                SELECT x.product_id FROM `{DATASET}.order_items` AS x
            ) ORDER BY x.cost
        """),
            [(3,), (4,)],
        )

    def test_all_original_table_occurrences_are_scoped(self):
        sql = f"""SELECT p.id FROM `{DATASET}.products` AS p
            JOIN `{DATASET}.products` AS q ON p.id = q.id"""
        result = parse_one(apply_brand_scope(sql), read="bigquery")
        self.assertEqual(len(list(result.find_all(exp.Subquery))), 2)
        self.assertEqual(len(list(result.find_all(exp.Parameter))), 2)

    def test_brand_values_are_not_embedded(self):
        result = apply_brand_scope(f"SELECT p.id FROM `{DATASET}.products` AS p")
        self.assertIn("@allowed_brands", result)
        self.assertNotIn("Brand A", result)

    def test_blocked_columns_are_not_projected_by_generated_sources(self):
        result = parse_one(apply_brand_scope(f"SELECT u.id FROM `{DATASET}.users` AS u"), read="bigquery")
        self.assertNotIn("email", {column.name for column in result.find_all(exp.Column)})
        self.assertNotIn("first_name", {column.name for column in result.find_all(exp.Column)})

    def test_rejections(self):
        cases = {
            "empty": "",
            "delete": f"DELETE FROM `{DATASET}.users` WHERE id = 1",
            "multiple": "SELECT 1; SELECT 2",
            "star": f"SELECT * FROM `{DATASET}.users`",
            "qualified_star": f"SELECT u.* FROM `{DATASET}.users` AS u",
            "pii": f"SELECT u.email FROM `{DATASET}.users` AS u",
            "renamed_pii": f"SELECT u.email AS revenue FROM `{DATASET}.users` AS u",
            "pii_in_filter": f"SELECT u.id FROM `{DATASET}.users` AS u WHERE u.email = 'x'",
            "unknown_column": f"SELECT u.fake FROM `{DATASET}.users` AS u",
            "ambiguous_column": f"SELECT id FROM `{DATASET}.users` AS u JOIN `{DATASET}.products` AS p ON u.id = p.id",
            "unqualified_pii": f"SELECT email FROM `{DATASET}.users` AS u",
            "unqualified_demographics": f"SELECT age FROM `{DATASET}.users` AS u",
            "wrong_project": "SELECT u.id FROM `other-project.thelook_ecommerce.users` AS u",
            "wrong_dataset": "SELECT u.id FROM `bigquery-public-data.other.users` AS u",
            "unknown_table": f"SELECT x.id FROM `{DATASET}.payments` AS x",
            "demographics_pending": f"SELECT u.age FROM `{DATASET}.users` AS u",
            "whole_order_count": f"SELECT o.num_of_item FROM `{DATASET}.orders` AS o",
            "model_parameter": f"SELECT p.id FROM `{DATASET}.products` AS p WHERE p.brand = @brand",
            "unknown_function": f"SELECT unknown_function(p.id) FROM `{DATASET}.products` AS p",
            "duplicate_alias": f"SELECT 1 FROM `{DATASET}.products` AS p JOIN `{DATASET}.users` AS p ON 1 = 1",
            "pii_in_cte": f"WITH x AS (SELECT u.email AS revenue FROM `{DATASET}.users` u) SELECT x.revenue FROM x",
            "encoded_identity_function": "SELECT TO_BASE64(CAST(SESSION_USER() AS BYTES)) AS encoded",
            "nested_pii": f"""SELECT p.id FROM `{DATASET}.products` AS p WHERE EXISTS (
                SELECT u.id FROM `{DATASET}.users` AS u WHERE u.email = 'x')""",
            "syntax_error": "SELECT (",
        }
        for name, sql in cases.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                apply_brand_scope(sql)

    def test_constant_select_is_allowed(self):
        validate_query("SELECT 1")

    def test_rewriting_twice_is_rejected(self):
        scoped = apply_brand_scope(f"SELECT p.id FROM `{DATASET}.products` AS p")
        with self.assertRaises(ValueError):
            apply_brand_scope(scoped)
