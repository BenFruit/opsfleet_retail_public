import os
import unittest
from uuid import uuid4

from retail_agent.reports.models import ReportRequest
from retail_agent.reports.repository import ReportRepository
from retail_agent.settings import Settings
from retail_agent.storage.postgres import connection_factory

VECTOR = [1.0] + [0.0] * 767


@unittest.skipUnless(os.getenv("RETAIL_TEST_POSTGRES") == "1", "Opt-in local pgvector integration test")
class ReportPostgresTests(unittest.TestCase):
    def test_real_storage_owner_scope_search_and_frozen_delete(self):
        repo = ReportRepository(connection_factory(Settings()))
        owner = "test_" + uuid4().hex
        brands = ["Demo Brand"]
        ids = [uuid4().hex for _ in range(3)]
        conversation = uuid4().hex

        def save(i, user=owner):
            repo.save(
                report_id=ids[i],
                owner=user,
                brands=brands,
                conversation_id=conversation,
                title="Nike demo",
                content="A historical Nike report",
                vector=VECTOR,
                model="gemini-embedding-2",
            )

        try:
            save(0)
            save(1, owner + "_other")
            self.assertIsNone(repo.open(ids[0], owner=owner + "_other", brands=brands))
            self.assertIsNone(repo.open(ids[0], owner=owner, brands=["Other Brand"]))
            found = repo.search(owner=owner, brands=brands, query="Nike", vector=VECTOR, model="gemini-embedding-2")
            self.assertEqual([r["report_id"] for r in found], [ids[0]])
            preview = repo.preview_delete(
                ReportRequest(operation="prepare_delete", query="Nike"),
                owner=owner,
                brands=brands,
                conversation_id=conversation,
            )
            save(2)  # Arrived after preview; must survive confirmation.
            self.assertEqual(repo.delete(tuple(r["report_id"] for r in preview), owner=owner, brands=brands), 1)
            self.assertIsNotNone(repo.open(ids[2], owner=owner, brands=brands))
            self.assertEqual(
                repo.preview_delete(
                    ReportRequest(operation="prepare_delete", query="' OR true --"),
                    owner=owner,
                    brands=brands,
                    conversation_id=conversation,
                ),
                [],
            )
        finally:
            repo.delete(tuple(ids), owner=owner, brands=brands)
            repo.delete(tuple(ids), owner=owner + "_other", brands=brands)
