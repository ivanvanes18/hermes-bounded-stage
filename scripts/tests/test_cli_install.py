"""Packaging and command interface tests; no real Hermes profile is touched."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
try:
    import install_skill as installer
except ImportError:
    installer = None


class CLITests(unittest.TestCase):
    def setUp(self):
        self.cli = SCRIPTS / "stage_cli.py"
        self.assertTrue(self.cli.exists(), "stage_cli implementation is missing")
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def invoke(self, *args, env=None):
        proc = subprocess.run([sys.executable, "-B", str(self.cli), *map(str, args)],
                              capture_output=True, text=True, env=env, timeout=20)
        return proc, json.loads(proc.stdout)

    def test_doctor_does_not_print_key(self):
        env = dict(os.environ); env["TYPESAFE_API_KEY"] = "TEST_ONLY_SECRET_DO_NOT_PRINT"
        proc, result = self.invoke("doctor", env=env)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(result["typesafe_key_present"])
        self.assertNotIn(env["TYPESAFE_API_KEY"], proc.stdout + proc.stderr)
        self.assertFalse(result["hermes_runtime_verified"])

    def test_offline_demo(self):
        proc, result = self.invoke("demo", "--workspace", self.root)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(result["status"], "ready_for_parent_review")
        self.assertEqual(result["jev_calls"], 0)
        self.assertEqual(len(result["completed"]), 2)

    def test_generation_demo_then_submit(self):
        proc, pending = self.invoke("demo", "--workspace", self.root, "--generation")
        self.assertEqual(proc.returncode, 10)
        self.assertEqual(pending["status"], "awaiting_generation")
        packet = json.loads(Path(pending["pending"]["packet_path"]).read_text())
        generated = json.loads(packet["context"]["sources"]["source"])
        generated["note"] = "Synthetic offline fixture, not a model response"
        Path(pending["pending"]["submission_path"]).write_text(json.dumps(generated), encoding="utf-8")
        proc, complete = self.invoke("submit", "--run", pending["run_directory"], "--nonce", pending["pending"]["nonce"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(complete["status"], "ready_for_parent_review")
        self.assertEqual(len(complete["completed"]), 2)

    def test_smoke_requires_explicit_network_flag(self):
        proc, result = self.invoke("smoke")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(result["reason"], "typesafe_not_authorized")

    def test_invalid_plan_returns_structured_failure(self):
        file = self.root / "bad.json"; file.write_text("{}")
        proc, result = self.invoke("validate", "--plan", file)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    def test_hash(self):
        file = self.root / "text"; file.write_text("abc")
        proc, result = self.invoke("hash", file)
        self.assertEqual(result["sha256"], hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(proc.returncode, 0)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(installer, "installer implementation is missing")
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"; self.bundle.mkdir()
        self.skill = self.bundle / "hermes-bounded-stage"; self.skill.mkdir()
        (self.skill / "SKILL.md").write_text(
            "---\nname: hermes-bounded-stage\nversion: %s\ndescription: Test fixture\n---\n" % installer.VERSION)
        (self.skill / "payload.txt").write_text("fixture")
        self.home = self.root / "profile"; self.home.mkdir()
        (self.home / "config.yaml").write_bytes(b"model: existing\n")
        (self.home / "MEMORY.md").write_bytes(b"keep memory\n")
        self.manifest()

    def manifest(self):
        data = {"schema_version": 1, "name": "hermes-bounded-stage", "version": installer.VERSION, "files": {}}
        for path in self.bundle.rglob("*"):
            if path.is_file() and path.name != "MANIFEST.json":
                data["files"][str(path.relative_to(self.bundle))] = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.bundle / "MANIFEST.json").write_text(json.dumps(data))

    def test_installs_only_skill(self):
        result = installer.install_bundle(self.bundle, self.home)
        target = self.home / "skills" / "hermes-bounded-stage"
        self.assertTrue((target / "SKILL.md").is_file())
        self.assertEqual(result["status"], "installed")
        self.assertEqual((self.home / "config.yaml").read_bytes(), b"model: existing\n")
        self.assertEqual((self.home / "MEMORY.md").read_bytes(), b"keep memory\n")
        self.assertEqual({p.name for p in self.home.iterdir()}, {"skills", "config.yaml", "MEMORY.md"})

    def test_existing_skill_never_overwritten(self):
        installer.install_bundle(self.bundle, self.home)
        with self.assertRaises(installer.InstallError): installer.install_bundle(self.bundle, self.home)

    def test_modified_payload_rejected(self):
        (self.skill / "payload.txt").write_text("tampered")
        with self.assertRaises(installer.InstallError): installer.install_bundle(self.bundle, self.home)
        self.assertFalse((self.home / "skills").exists())

    def test_unlisted_file_rejected(self):
        (self.skill / "unlisted.py").write_text("pass")
        with self.assertRaises(installer.InstallError): installer.verify_bundle(self.bundle)

    def test_symlink_rejected(self):
        (self.skill / "payload.txt").unlink()
        (self.skill / "payload.txt").symlink_to(self.home / "MEMORY.md")
        with self.assertRaises(installer.InstallError): installer.verify_bundle(self.bundle)

    def test_manifest_traversal_rejected(self):
        data = json.loads((self.bundle / "MANIFEST.json").read_text())
        data["files"]["../outside"] = "0" * 64
        (self.bundle / "MANIFEST.json").write_text(json.dumps(data))
        with self.assertRaises(installer.InstallError): installer.verify_bundle(self.bundle)

    def test_skills_symlink_rejected(self):
        other = self.root / "other"; other.mkdir()
        (self.home / "skills").symlink_to(other)
        with self.assertRaises(installer.InstallError): installer.install_bundle(self.bundle, self.home)
        self.assertEqual(list(other.iterdir()), [])

    def test_missing_profile_not_created(self):
        missing = self.root / "missing"
        with self.assertRaises(installer.InstallError): installer.install_bundle(self.bundle, missing)
        self.assertFalse(missing.exists())

    def test_verify_only_does_not_install(self):
        result = installer.verify_bundle(self.bundle)
        self.assertEqual(result["name"], "hermes-bounded-stage")
        self.assertFalse((self.home / "skills").exists())


if __name__ == "__main__":
    unittest.main()
