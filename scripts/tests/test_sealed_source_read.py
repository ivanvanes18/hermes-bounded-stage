"""RED regression: classification must use the exact bytes the sealed hash covered.

Finding: `bounded_runtime._filter` hashes the source with `sha256_file` and then parses it
again with `read_json`. Those are two separate reads of a pathname. A same-user process can
present different bytes to the second read and restore the file before the post-batch
recheck, so Jev classifies — and the output records — bytes that never matched the sealed
hash, while every hash check still passes.

The swap is simulated deterministically at the read boundary; no real race is needed and the
file on disk is never modified. The artifact's JSON-limit read (the parse) is served swapped
bytes while every hash read sees the true sealed file, which is exactly what swap-and-restore
looks like from the controller's side. Both the `runtime_support` and `bounded_runtime`
bindings of `read_regular` are patched, so the interception holds however the read is reached,
and the test asserts the interception actually fired rather than passing vacuously.

Desired fix: read the exact bytes once, verify that buffer against the recorded source hash,
and parse that same buffer. Rechecks stay fail-closed.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
try:
    import bounded_runtime as rt
    import runtime_support as rs
except ImportError:  # pragma: no cover - mirrors the other suites
    rt = rs = None

OK_FLAGS = {"unit_matches": True, "quantity_matches": True}


class SealedJev:
    """Would answer either the sealed or the swapped pair, so the run is not saved by luck."""

    def __init__(self):
        self.calls = []

    def evaluate(self, state, questions):
        self.calls.append(dict(questions))
        answers = {}
        for key, question in questions.items():
            keys = list(question["criteria"])
            probabilities = {name: 0.005 for name in keys}
            probabilities["exact"] = 0.99
            answers[key] = {"type": "choice", "choice": "exact", "confidence": 0.98,
                            "probabilities": probabilities}
        return {"model": "jev-1.13.0", "answers": answers,
                "usage": {"input_tokens": 100, "output_tokens": 10}}


class SealedSourceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(rt, "bounded_runtime implementation is missing")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifact = self.root / "candidates.json"
        self.write(self.artifact, {"schema_version": 2, "items": [
            {"item_id": "i1", "text": "Клапан X1 — 12 шт.", "candidates": [
                {"candidate_id": "c1", "text": "Клапан X1 — 12 шт.", "flags": dict(OK_FLAGS)}]}]})
        plan = {"schema_version": 2, "stage_id": "sealed-source",
                "goal": "Classify supplied candidate pairs and surface exceptions",
                "constraints": ["Do not invent candidates, quantities, or units"],
                "inputs": {"candidates": {"path": str(self.artifact),
                                          "sha256": hashlib.sha256(self.artifact.read_bytes()).hexdigest()}},
                "steps": [{"id": "filter", "output": "decisions.json", "context": ["candidates"],
                           "actions": [{"id": "classify", "kind": "semantic_exception_filter",
                                        "description": "Classify each supplied pair",
                                        "source": "candidates",
                                        "instructions": "Does the candidate describe the same scope of work?",
                                        "required_flags": ["unit_matches", "quantity_matches"],
                                        "batch_size": 2}],
                           "checks": [{"kind": "json"}], "semantic_checks": []}],
                "limits": {"max_jev_calls": 8, "max_generations": 0, "max_seconds": 600}}
        self.plan_path = self.root / "plan.json"
        self.write(self.plan_path, plan)
        self.run = self.root / "run"
        rt.initialize(self.plan_path, self.run, allow_typesafe=True)

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_parse_cannot_see_bytes_the_sealed_hash_never_covered(self):
        swapped = json.dumps({"schema_version": 2, "items": [
            {"item_id": "i1", "text": "ПОДМЕНА", "candidates": [
                {"candidate_id": "c9", "text": "ПОДМЕНА", "flags": dict(OK_FLAGS)}]}]},
            ensure_ascii=False).encode("utf-8")
        target = (self.run / "inputs" / "candidates.json").resolve()
        real = rs.read_regular
        intercepted = []

        def reader(path, maximum=rs.MAX_FILE_BYTES):
            # Only the parse read is swapped; every hash read still sees the sealed file.
            if maximum == rs.MAX_JSON_BYTES and Path(path).resolve() == target:
                intercepted.append(maximum)
                return swapped
            return real(path, maximum)

        client = SealedJev()
        with patch.object(rs, "read_regular", reader), patch.object(rt, "read_regular", reader):
            result = rt.advance(self.run, client=client)

        self.assertEqual(len(intercepted), 1,
                         "the sealed source was not read exactly once under the JSON limit")
        asked = {key for questions in client.calls for key in questions}
        self.assertLessEqual(asked, {"i1:c1"},
                             "Jev was asked about a pair that is not in the sealed source")
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("input_changed", result["reason"] or "")
        self.assertNotIn("filter", result["completed"])
        self.assertFalse((self.run / "artifacts" / "filter" / "decisions.json").exists())

    def test_post_batch_hash_recheck_catches_mutation_independently(self):
        """The explicit post-provider hash check remains effective even without the broad freshness sweep."""
        target = self.run / "inputs" / "candidates.json"

        class MutatingJev(SealedJev):
            def evaluate(inner_self, state, questions):
                reply = super(MutatingJev, inner_self).evaluate(state, questions)
                target.write_text('{"schema_version":2,"items":[]}', encoding="utf-8")
                return reply

        client = MutatingJev()
        original_fresh = rt._load_fresh

        def broad_freshness(run, state):
            if client.calls:
                return None, None  # Calls after the provider intentionally bypass the broad sweep.
            return original_fresh(run, state)

        with patch.object(rt, "_load_fresh", broad_freshness):
            result = rt.advance(self.run, client=client)

        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason"], "input_changed:candidates")
        self.assertNotIn("filter", result["completed"])
        self.assertFalse((self.run / "artifacts" / "filter" / "decisions.json").exists())


if __name__ == "__main__":
    unittest.main()
