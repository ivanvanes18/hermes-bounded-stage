"""RED regression: the bundle must reject SKILL.md frontmatter that contradicts the manifest.

Finding: `install_skill.verify_bundle` checks the manifest `version` and the SKILL.md `name`,
but never the SKILL.md `version:` line. A bundle can therefore declare 1.1.0 in MANIFEST.json,
pass verification, and install a SKILL.md that still announces 1.0.0 to Hermes.

Both expected versions are independent literals on purpose. Deriving them from
`installer.VERSION` would make the fixture agree with the code by construction and prove
nothing about a mismatch. The precondition assertion keeps the fixture honest: if the shipped
version moves, this test fails loudly instead of silently rejecting for the wrong reason.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
try:
    import install_skill as installer
except ImportError:  # pragma: no cover - mirrors the other suites
    installer = None

NAME = "hermes-bounded-stage"
MANIFEST_VERSION = "1.4.0"   # what the shipped bundle declares
STALE_VERSION = "1.0.0"      # frontmatter left behind inside the same bundle


class BundleSkillVersionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(installer, "installer implementation is missing")
        self.assertEqual(installer.VERSION, MANIFEST_VERSION,
                         "fixture pins the shipped version literally; update it deliberately")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        self.skill = self.bundle / NAME
        self.skill.mkdir(parents=True)
        (self.skill / "payload.txt").write_text("fixture")
        self.home = self.root / "profile"
        self.home.mkdir()

    def build(self, skill_version):
        (self.skill / "SKILL.md").write_text(
            "---\nname: %s\nversion: %s\ndescription: Test fixture\n---\n" % (NAME, skill_version))
        data = {"schema_version": 1, "name": NAME, "version": MANIFEST_VERSION, "files": {}}
        for path in self.bundle.rglob("*"):
            if path.is_file() and path.name != "MANIFEST.json":
                data["files"][str(path.relative_to(self.bundle))] = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.bundle / "MANIFEST.json").write_text(json.dumps(data))

    def test_matching_skill_version_is_still_accepted(self):
        # Control: the rejection below must be about the mismatch, not about every bundle.
        self.build(MANIFEST_VERSION)
        self.assertEqual(installer.verify_bundle(self.bundle)["version"], MANIFEST_VERSION)

    def test_stale_skill_version_is_rejected(self):
        self.build(STALE_VERSION)
        with self.assertRaises(installer.InstallError) as caught:
            installer.verify_bundle(self.bundle)
        self.assertEqual(str(caught.exception), "skill_version_mismatch")

    def test_stale_skill_version_is_never_installed(self):
        self.build(STALE_VERSION)
        with self.assertRaises(installer.InstallError):
            installer.install_bundle(self.bundle, self.home)
        self.assertFalse((self.home / "skills").exists())


if __name__ == "__main__":
    unittest.main()
