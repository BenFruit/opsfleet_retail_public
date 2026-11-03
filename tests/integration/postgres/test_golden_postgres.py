import json
import os
import unittest
from uuid import uuid4

from retail_agent.golden.ingest import DEFAULT_BUCKET
from retail_agent.golden.models import GoldenTrio
from support.paths import REPO_ROOT

BUCKET = REPO_ROOT / DEFAULT_BUCKET
VECTOR = [0.0] * 767 + [1.0]


def trio(**changes):
    data = json.loads((BUCKET / "monthly-revenue-trend.json").read_text(encoding="utf-8"))
    data.update(changes)
    return GoldenTrio.model_validate(data)


@unittest.skipUnless(os.getenv("RETAIL_TEST_POSTGRES") == "1", "Opt-in local pgvector integration test")
class GoldenPostgresTests(unittest.TestCase):
    def test_writer_publishes_reader_searches_and_reader_cannot_write(self):
        from retail_agent.golden.repository import GoldenRepository
        from retail_agent.settings import Settings
        from retail_agent.storage.postgres import StorageError, connection_factory, golden_ingest_connection_factory

        writer = GoldenRepository(golden_ingest_connection_factory(Settings()))
        reader = GoldenRepository(connection_factory(Settings()))
        test_trio = trio(id="test-" + uuid4().hex[:12])
        try:
            writer.publish(upserts=[(test_trio, VECTOR)], remove=[], model="test-model")
            found = reader.search(vector=VECTOR, query_text="monthly revenue", model="test-model")
            self.assertEqual(found[0]["trio_id"], test_trio.id)
            self.assertAlmostEqual(found[0]["similarity"], 1.0, places=5)
            self.assertGreater(found[0]["lexical"], 0)
            self.assertEqual(GoldenTrio.model_validate(found[0]["document"]), test_trio)
            with self.assertRaises(StorageError):  # Chat agent role has SELECT only.
                reader.publish(upserts=[(test_trio, VECTOR)], remove=[], model="test-model")
        finally:
            writer.publish(upserts=[], remove=[test_trio.id], model="test-model")
        self.assertNotIn(test_trio.id, writer.indexed_hashes())
