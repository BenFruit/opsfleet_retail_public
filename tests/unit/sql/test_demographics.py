import json
import sqlite3
import unittest
from unittest.mock import Mock

from sqlglot import exp, parse_one

from retail_agent.agent.models import AgentAction
from retail_agent.agent.orchestrator import AnalyticalAgent
from retail_agent.data.bigquery_runner import BigQueryRunner, QueryExecution
from retail_agent.data.query_service import QueryLimits, QueryResult, QueryService
from retail_agent.llm.analyst import Analyst
from retail_agent.llm.client import GeminiClient
from retail_agent.safety.errors import ResultGuardError
from retail_agent.safety.result_guard import make_observation
from retail_agent.schema.catalog import TABLE_POLICIES
from retail_agent.sql.demographics import COHORT_COLUMN
from retail_agent.sql.scope import apply_brand_scope
from retail_agent.sql.validator import validate_query
from support.demo_access import DEMO_ENTITLEMENTS

PATH = "bigquery-public-data.thelook_ecommerce"
FROM = f"FROM `{PATH}.order_items` oi JOIN `{PATH}.users` u ON oi.user_id=u.id JOIN `{PATH}.products` p ON oi.product_id=p.id"


class DemographicSQLTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        for table, columns in TABLE_POLICIES.items():
            fields = ", ".join(f'"{name}"' for name in columns)
            self.db.execute(f'CREATE TABLE "{table}" ({fields})')
        self.db.executemany(
            "INSERT INTO products (id,brand,name,cost) VALUES (?,?,?,?)",
            [
                (1, "Brand A", "Visible product", 2),
                (2, "Brand C", "Other product", 3),
            ],
        )
        self.db.executemany(
            "INSERT INTO users (id,age,state) VALUES (?,?,?)",
            [
                *[(i, 24, "CA") for i in range(1, 7)],
                (7, 24, "NY"),
                (8, 25, "NY"),
                (9, 24, "TX"),
            ],
        )
        items = [(i, i, 1, 10, "Complete") for i in range(1, 9)]
        items += [(10 + i, 1, 1, 10, "Complete") for i in range(4)]
        items += [(20 + i, 7, 1, 1000, "Complete") for i in range(5)]
        items += [(40 + i, 9, 2, 10, "Complete") for i in range(20)]
        self.db.executemany(
            "INSERT INTO order_items (id,user_id,product_id,sale_price,status) VALUES (?,?,?,?,?)", items
        )

    def execute(self, sql, minimum=5, brands=("Brand A",)):
        qualified = validate_query(sql)
        demographic = bool(qualified.meta.get("demographic_subject"))
        tree = parse_one(apply_brand_scope(sql, minimum_cohort_size=minimum), read="bigquery")
        for predicate in list(tree.find_all(exp.In)):
            if predicate.args.get("unnest") is not None:
                predicate.replace(
                    exp.In(this=predicate.this.copy(), expressions=[exp.Literal.string(b) for b in brands])
                )
        for table in tree.find_all(exp.Table):
            table.set("catalog", None)
            table.set("db", None)
        cursor = self.db.execute(tree.sql(dialect="sqlite"))
        columns = [c[0] for c in cursor.description]
        rows = tuple(dict(zip(columns, row, strict=True)) for row in cursor.fetchall())
        result = QueryResult(
            "id", rows, False, 1, 1, 1, minimum if demographic else None, tuple(qualified.named_selects)
        )
        return result, make_observation(sql, result, max_chars=12000)

    def test_exact_age_and_range_are_distinct_authorized_buyers(self):
        for condition, count in (("u.age=24", 7), ("u.age BETWEEN 20 AND 30", 8)):
            _, observation = self.execute(f"SELECT COUNT(DISTINCT u.id) AS buyers {FROM} WHERE {condition}")
            self.assertEqual(observation["rows"], [{"buyers": count}])
            self.assertNotIn(COHORT_COLUMN, json.dumps(observation))

    def test_five_year_buckets_and_rounding_are_model_sql_not_new_business_parameters(self):
        _, observation = self.execute(
            f"SELECT FLOOR(u.age/5)*5 AS age_from, FLOOR(u.age/5)*5+4 AS age_to, p.brand, COUNT(DISTINCT u.id) AS buyers, ROUND(SUM(oi.sale_price),2) AS revenue {FROM} GROUP BY age_from, age_to, p.brand ORDER BY age_from"
        )
        self.assertEqual(
            observation["rows"], [{"age_from": 20, "age_to": 24, "brand": "Brand A", "buyers": 7, "revenue": 5110.0}]
        )

    def test_aggregate_ratio_and_ten_year_buckets_use_same_policy(self):
        _, observation = self.execute(
            f"SELECT FLOOR(u.age/10)*10 AS decade, ROUND(SUM(oi.sale_price)/COUNT(DISTINCT u.id),2) AS spend {FROM} GROUP BY decade"
        )
        self.assertEqual(observation["rows"][0]["decade"], 20)
        self.assertIn("spend", observation["rows"][0])

    def test_suppression_precedes_order_limit(self):
        _, observation = self.execute(
            f"SELECT u.state, SUM(oi.sale_price) AS revenue {FROM} GROUP BY u.state ORDER BY revenue DESC LIMIT 1"
        )
        self.assertEqual(observation["rows"], [{"state": "CA", "revenue": 100}])

    def test_bucket_can_be_publishable_even_when_exact_ages_are_suppressed(self):
        self.db.executemany("UPDATE users SET age=? WHERE id=?", [(19 + i, i) for i in range(1, 7)])
        _, exact = self.execute(f"SELECT u.age, COUNT(DISTINCT u.id) AS buyers {FROM} GROUP BY u.age")
        _, buckets = self.execute(
            f"SELECT FLOOR(u.age/5)*5 AS age_from, COUNT(DISTINCT u.id) AS buyers {FROM} GROUP BY age_from"
        )
        self.assertEqual(exact["rows"], [])
        self.assertEqual(buckets["rows"], [{"age_from": 20, "buyers": 6}])

    def test_many_purchases_by_one_buyer_do_not_make_safe_cohort(self):
        _, observation = self.execute(f"SELECT COUNT(DISTINCT u.id) AS buyers {FROM} WHERE u.age=24 AND u.state='NY'")
        self.assertEqual(observation["rows"], [])

    def test_nullable_inputs_require_actual_contributors(self):
        self.db.execute("UPDATE users SET age=NULL WHERE id<=5")
        _, observation = self.execute(f"SELECT AVG(u.age) AS mean_age {FROM}")
        self.assertEqual(observation["rows"], [])

    def test_derived_null_metric_inputs_do_not_inflate_contributors(self):
        _, observation = self.execute(f"SELECT AVG(NULLIF(u.age,24)) AS mean_age {FROM}")
        self.assertEqual(observation["rows"], [])

    def test_configurable_threshold_and_custom_having(self):
        _, observation = self.execute(
            f"SELECT u.state, COUNT(DISTINCT u.id) AS buyers {FROM} GROUP BY u.state HAVING COUNT(DISTINCT u.id)>1 ORDER BY buyers DESC",
            minimum=2,
        )
        self.assertEqual([r["state"] for r in observation["rows"]], ["CA", "NY"])

    def test_product_revenue_without_demographics_has_no_threshold(self):
        result, observation = self.execute(
            f"SELECT SUM(oi.sale_price) AS revenue FROM `{PATH}.order_items` oi", brands=("Brand C",)
        )
        self.assertIsNone(result.minimum_cohort_size)
        self.assertEqual(observation["rows"], [{"revenue": 200}])

    def test_identifier_laundering_targeting_and_complex_shapes_fail_closed(self):
        queries = [
            f"SELECT u.id, AVG(u.age) AS age {FROM} GROUP BY u.id",
            f"SELECT AVG(u.age) AS age {FROM} WHERE u.id=1",
            f"SELECT u.id AS customer {FROM} WHERE u.age=24",
            f"SELECT oi.order_id, u.age {FROM} GROUP BY oi.order_id,u.age",
            f"SELECT SUM(CASE WHEN u.id=1 THEN u.age ELSE 0 END) AS age {FROM}",
            f"SELECT AVG(CASE WHEN u.age>24 THEN u.age END) AS age {FROM}",
            f"SELECT COUNTIF(u.age=24) AS buyers {FROM}",
            f"SELECT AVG(u.age) AS age {FROM} WHERE oi.id=1",
            f"SELECT u.age AS revenue {FROM}",
            f"WITH x AS (SELECT u.age FROM `{PATH}.users` u) SELECT AVG(age) FROM x",
            f"SELECT u.age, SUM(oi.sale_price) OVER () AS revenue {FROM}",
            f"SELECT u.age, COUNT(*) AS n {FROM} GROUP BY u.age,oi.inventory_item_id",
            f"SELECT AVG(u.age) AS age FROM `{PATH}.users` u JOIN `{PATH}.order_items` oi ON oi.id=u.id",
            f"SELECT u.email AS revenue, COUNT(*) AS n {FROM} GROUP BY u.email",
            f"SELECT COUNT(*) AS __cohort_size {FROM} WHERE u.age=24",
        ]
        for sql in queries:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                validate_query(sql)

    def test_non_demographic_individual_spending_stays_allowed(self):
        validate_query(
            f"SELECT oi.user_id, SUM(oi.sale_price) AS spend FROM `{PATH}.order_items` oi GROUP BY oi.user_id"
        )

    def test_second_guard_hides_small_values(self):
        result = QueryResult(
            "id",
            ({"state": "DO_NOT_PUBLISH", "revenue": 999, COHORT_COLUMN: 1},),
            False,
            1,
            1,
            1,
            5,
            ("state", "revenue"),
        )
        observation = make_observation("", result, max_chars=12000)
        self.assertNotIn("DO_NOT_PUBLISH", json.dumps(observation))

    def test_invalid_cohort_counts_fail_closed(self):
        for count in (None, True, "10", -1):
            with self.subTest(count=count), self.assertRaises(ResultGuardError):
                make_observation(
                    "",
                    QueryResult("id", ({"buyers": 10, COHORT_COLUMN: count},), False, 1, 1, 1, 5, ("buyers",)),
                    max_chars=1000,
                )

    def test_graph_uses_query_action_and_never_sends_suppressed_values(self):
        llm = Mock(spec=GeminiClient)
        llm.last_usage = {}
        llm.generate_structured.side_effect = [
            AgentAction(action="query", sql=f"SELECT u.state, SUM(oi.sale_price) AS revenue {FROM} GROUP BY u.state"),
            AgentAction(action="answer", answer="No publishable groups."),
        ]
        runner = Mock(spec=BigQueryRunner)
        runner.dry_run.return_value = 10
        runner.execute_scoped_query.return_value = QueryExecution(
            ({"state": "DO_NOT_PUBLISH", "revenue": 999, COHORT_COLUMN: 1},), False, 10, 10
        )
        agent = AnalyticalAgent(
            Analyst(llm, {}),
            QueryService(runner, QueryLimits(minimum_cohort_size=6)),
            manager_id="manager_a",
            entitlements=DEMO_ENTITLEMENTS,
        )
        turn = agent.ask("Which states have the most revenue?")
        prompt = llm.generate_structured.call_args.args[0]
        self.assertNotIn("DO_NOT_PUBLISH", prompt + turn.answer)
        self.assertNotIn(COHORT_COLUMN, prompt)
        self.assertIn(">= 6", runner.execute_scoped_query.call_args.args[0])
