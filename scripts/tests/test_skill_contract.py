"""Static documentation checks, NOT behavioral evaluation of an LLM."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]


class SkillContractTests(unittest.TestCase):
    def read(self, relative):
        path = ROOT / relative
        self.assertTrue(path.is_file(), f"Missing skill component: {relative}")
        return path.read_text(encoding="utf-8")

    def test_frontmatter_and_standard_sections(self):
        text = self.read("SKILL.md")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: hermes-bounded-stage", text)
        self.assertIn("version: 1.3.0", text)
        for heading in ("## When to Use", "## Procedure", "## Pitfalls", "## Verification"):
            self.assertIn(heading, text)
        description = re.search(r"(?m)^description: (.+)$", text).group(1)
        self.assertLessEqual(len(description), 60)

    def test_local_skill_links_exist(self):
        text = self.read("SKILL.md")
        links = re.findall(r"\]\(([^)]+)\)", text)
        self.assertGreaterEqual(len(links), 5)
        for link in links:
            if not link.startswith("https://"):
                self.assertTrue((ROOT / link).is_file(), link)

    def test_no_false_runtime_or_acceptance_claim(self):
        text = self.read("SKILL.md")
        self.assertIn("ready_for_parent_review", text)
        self.assertIn("не является песочницей", text)
        self.assertIn("delegation.model", text)

    def test_worker_contract_has_required_boundaries(self):
        text = self.read("templates/worker-context.md")
        for item in ("awaiting_generation", "needs_review", "ready_for_parent_review", "nonce", "state.json", "не выполняй"):
            self.assertIn(item, text)

    def test_parent_protocol_separates_provider_evidence(self):
        text = self.read("references/hermes-workflow.md")
        for item in ("delegate_task", "goal", "context", "delegation.model", "не измеряет", "не проверяет"):
            self.assertIn(item, text)

    def test_domain_integration_preserves_existing_rules(self):
        text = self.read("references/domain-integration.md")
        for item in ("smeta-vor-humanizer", "smeta-pir", "GBrain", "НДС", "same_numeric_tokens"):
            self.assertIn(item, text)


if __name__ == "__main__":
    unittest.main()
