from .catalog import ANALYSIS_TABLES, TABLE_POLICIES


class SchemaService:
    def __init__(self, bigquery_runner):
        self.bigquery_runner = bigquery_runner

    def get_table_schema(self, table_name: str) -> list[dict]:
        if table_name not in TABLE_POLICIES:
            raise ValueError(f"Table '{table_name}' is not allowed")

        physical_schema = self.bigquery_runner.get_table_schema(table_name)
        column_policies = TABLE_POLICIES[table_name]

        enriched_schema = []

        for column in physical_schema:
            column_name = column["name"]

            if column_name not in column_policies:
                raise ValueError(f"Missing policy for '{table_name}.{column_name}'")

            policy = column_policies[column_name]

            enriched_schema.append(
                {
                    **column,
                    "access_level": policy.access_level.value,
                }
            )

        return enriched_schema

    def load_analysis_schema(self) -> dict[str, list[dict]]:
        """Policy-annotated schema of every analysis table."""
        return {table: self.get_table_schema(table) for table in ANALYSIS_TABLES}
