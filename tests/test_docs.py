import re
import unittest

from support.paths import REPO_ROOT


class DocumentationTests(unittest.TestCase):
    def test_every_diagram_is_embedded_and_matches_its_source(self):
        # GitHub renders Mermaid inside Markdown, so each .mmd source must appear as a block somewhere.
        embedded = {
            "PRODUCTION_HLD.md": (
                "production-hld.mmd",
                "request-sequence.mmd",
                "learning-loop.mmd",
                "architecture-overview.mmd",
                "agent-loop.mmd",
            ),
            "QUERY_SAFETY.md": ("security-pii.mmd",),
            "SAVED_REPORTS.md": ("report-deletion.mmd",),
        }
        sources = {path.name for path in (REPO_ROOT / "HLD").glob("*.mmd")}
        self.assertEqual(sources, {name for names in embedded.values() for name in names})
        for document, names in embedded.items():
            text = (REPO_ROOT / "HLD" / document).read_text(encoding="utf-8")
            blocks = re.findall(r"```mermaid\n(.*?)\n```", text, re.S)
            for name in names:
                source = (REPO_ROOT / "HLD" / name).read_text(encoding="utf-8").rstrip("\n")
                with self.subTest(document=document, diagram=name):
                    self.assertIn(source, blocks)

    def test_relative_markdown_links_resolve(self):
        # Published documentation only; local working notes are not in the repository.
        for doc in [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "HLD").glob("*.md"))]:
            text = doc.read_text(encoding="utf-8")
            for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                with self.subTest(doc=doc.name, target=target):
                    self.assertTrue((doc.parent / target).exists())
