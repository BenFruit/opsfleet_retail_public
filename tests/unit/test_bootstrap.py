import unittest
from unittest.mock import Mock

from support.demo_access import DEMO_ENTITLEMENTS


class BootstrapTests(unittest.TestCase):
    def test_chat_and_evaluation_share_wiring_and_optional_parts(self):
        from retail_agent.bootstrap import build_agent

        connect, embedder = Mock(), Mock()
        full = build_agent(
            Mock(),
            {},
            Mock(),
            manager_id="manager_a",
            entitlements=DEMO_ENTITLEMENTS,
            connect=connect,
            embedder=embedder,
            golden=True,
            memory=True,
            persona_doc_id="doc",
            golden_min_similarity=0.7,
        )
        self.assertIs(full.reports.repository._connect, connect)
        self.assertIs(full.memory.embedder, embedder)
        self.assertIs(full.golden.repository.connection, connect)
        self.assertEqual(full.golden.min_similarity, 0.7)
        self.assertEqual(full.analyst.persona_style.reader.document, "doc")
        self.assertIsNone(
            build_agent(
                Mock(),
                {},
                Mock(),
                manager_id="manager_a",
                entitlements=DEMO_ENTITLEMENTS,
                connect=connect,
                embedder=embedder,
                golden=False,
                memory=False,
                persona_doc_id="",
                golden_min_similarity=0.65,
            ).analyst.persona_style.reader
        )
        self.assertEqual(full.allowed_brands, ["Brand A", "Brand B"])
        bare = build_agent(
            Mock(),
            {},
            Mock(),
            manager_id="manager_b",
            entitlements=DEMO_ENTITLEMENTS,
            connect=connect,
            embedder=embedder,
            golden=False,
            memory=False,
            persona_doc_id="",
            golden_min_similarity=0.65,
        )
        self.assertIsNone(bare.golden)
        self.assertIsNone(bare.memory)
        self.assertIsNotNone(bare.reports)
