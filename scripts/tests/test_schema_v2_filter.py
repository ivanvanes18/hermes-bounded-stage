"""RED tests for schema v2 `semantic_exception_filter` (hermes-bounded-stage 1.1).

Offline only. The fake provider replies are NOT live model evidence; these tests
pin the controller's composition rules, not Jev's domain quality.

Assumed v2 surface (this file is the executable statement of it; admission.py /
bounded_runtime.py must match it):

  plan.schema_version == 2 admits exactly one new action kind:
    {"id": ..., "kind": "semantic_exception_filter", "description": ...,
     "source": <context name of the candidate artifact>,
     "instructions": <classification instructions>,
     "required_flags": [<deterministic flag names that must all be true>],
     "batch_size": <max pairs per Jev call>,
     optional min_probability / min_confidence / min_margin}

  Candidate artifact (closed shape, produced by an admitted producer):
    {"schema_version": 2,
     "items": [{"item_id": ID, "text": STR,
                "candidates": [{"candidate_id": ID, "text": STR,
                                "flags": {NAME: bool, ...}}]}]}

  Step output (closed shape):
    {"schema_version": 2,
     "decisions": [{"item_id": ID, "candidate_id": ID, "match": "exact|partial|none",
                    "status": "covered|missing|exception", "reason": STR|null}],
     "exceptions": [{"item_id": ID, "candidate_id": ID, "reason": STR}]}

  Jev sees one fixed Choice question per pair, keyed "ITEM_ID:CANDIDATE_ID",
  with criteria exactly {"exact", "partial", "none"}.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
try:
    import admission
    import bounded_runtime as rt
except ImportError:  # pragma: no cover - mirrors test_runtime.py
    admission = rt = None

OPTIONS = {"exact", "partial", "none"}
OK_FLAGS = {"unit_matches": True, "quantity_matches": True}
BAD_FLAGS = {"unit_matches": True, "quantity_matches": False}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class FilterJev:
    """Answers one fixed exact/partial/none question per candidate pair."""

    def __init__(self, choices, confidence=0.98, probability=0.99, confidences=None, corrupt=False):
        self.choices = dict(choices)
        self.confidence = confidence
        self.probability = probability
        self.confidences = dict(confidences or {})
        self.corrupt = corrupt
        self.calls = []

    def evaluate(self, state, questions):
        self.calls.append((copy.deepcopy(state), copy.deepcopy(questions)))
        answers = {}
        for key, question in questions.items():
            if key not in self.choices:
                raise AssertionError(
                    "unexpected question key %r; expected one of %s" % (key, sorted(self.choices)))
            keys = list(question["criteria"])
            chosen = self.choices[key]
            probabilities = {k: (1 - self.probability) / (len(keys) - 1) for k in keys}
            probabilities[chosen] = self.probability
            answers[key] = {"type": "choice", "choice": chosen,
                            "confidence": self.confidences.get(key, self.confidence),
                            "probabilities": probabilities}
        if self.corrupt:
            # A single malformed answer must never be read as an approval.
            next(iter(answers.values()))["choice"] = "covered"
        return {"model": "jev-1.13.0", "answers": answers,
                "usage": {"input_tokens": 100, "output_tokens": 10}}


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(rt, "bounded_runtime implementation is missing")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifact_path = self.root / "candidates.json"
        self.plan_path = self.root / "plan.json"
        self.run = self.root / "run"
        self.write_artifact(("i1", "c1", OK_FLAGS))
        self.plan = {
            "schema_version": 2, "stage_id": "filter-stage",
            "goal": "Classify supplied candidate pairs and surface exceptions",
            "constraints": ["Do not invent candidates, quantities, or units"],
            "inputs": {"candidates": {"path": str(self.artifact_path), "sha256": "0" * 64}},
            "steps": [{"id": "filter", "output": "decisions.json", "context": ["candidates"],
                       "actions": [{"id": "classify", "kind": "semantic_exception_filter",
                                    "description": "Classify each supplied pair as exact, partial or none",
                                    "source": "candidates",
                                    "instructions": "Does the candidate describe the same scope of work as the item?",
                                    "required_flags": ["unit_matches", "quantity_matches"],
                                    "batch_size": 2}],
                       "checks": [{"kind": "json"},
                                  {"kind": "json_has_keys", "fields": ["decisions", "exceptions"]}],
                       "semantic_checks": []}],
            "limits": {"max_jev_calls": 8, "max_generations": 0, "max_seconds": 600}}

    def write_artifact(self, *pairs):
        items = {}
        for item_id, candidate_id, flags in pairs:
            items.setdefault(item_id, []).append(
                {"candidate_id": candidate_id, "text": "Клапан %s — 12 шт." % candidate_id,
                 "flags": dict(flags)})
        dump(self.artifact_path, {"schema_version": 2, "items": [
            {"item_id": name, "text": "Клапан %s — 12 шт." % name, "candidates": candidates}
            for name, candidates in items.items()]})

    def start(self, allow=True):
        # Hash after the artifact is final, so pair layout and sealing stay independent.
        self.plan["inputs"]["candidates"]["sha256"] = digest(self.artifact_path)
        dump(self.plan_path, self.plan)
        return rt.initialize(self.plan_path, self.run, allow_typesafe=allow)

    def decisions(self, result):
        data = json.loads(Path(result["completed"]["filter"]["path"]).read_text(encoding="utf-8"))
        return data, {(d["item_id"], d["candidate_id"]): d for d in data["decisions"]}

    # --- admission -------------------------------------------------------

    def test_v1_plan_remains_admitted(self):
        source = self.root / "источник.txt"
        source.write_text("Поставка клапана X1 — 12 шт.\n", encoding="utf-8")
        plan = {"schema_version": 1, "stage_id": "sample", "goal": "Prepare a checked copy",
                "constraints": ["Do not change quantities or units"],
                "inputs": {"source": {"path": str(source), "sha256": digest(source)}},
                "steps": [{"id": "first", "output": "результат.txt", "context": ["source"],
                           "actions": [{"id": "keep", "kind": "copy", "source": "source",
                                        "description": "Keep exactly"}],
                           "checks": [{"kind": "same_sha256", "source": "source"}],
                           "semantic_checks": []}],
                "limits": {"max_jev_calls": 8, "max_generations": 2, "max_seconds": 600}}
        dump(self.plan_path, plan)
        admission.validate_plan(copy.deepcopy(plan))
        rt.initialize(self.plan_path, self.run)
        result = rt.advance(self.run)
        self.assertEqual(result["status"], "ready_for_parent_review")
        self.assertEqual(result["jev_calls"], 0)
        self.assertEqual(result["completed"]["first"]["action_id"], "keep")

    def test_v2_plan_is_admitted(self):
        self.plan["inputs"]["candidates"]["sha256"] = digest(self.artifact_path)
        admission.validate_plan(copy.deepcopy(self.plan))

    def test_filter_action_not_admitted_in_v1_plan(self):
        self.plan["schema_version"] = 1
        self.plan["inputs"]["candidates"]["sha256"] = digest(self.artifact_path)
        with self.assertRaises(rt.StageError):
            admission.validate_plan(copy.deepcopy(self.plan))

    # --- composition -----------------------------------------------------

    def test_exact_with_all_flags_true_is_covered(self):
        self.start()
        fake = FilterJev({"i1:c1": "exact"})
        result = rt.advance(self.run, client=fake)
        self.assertEqual(result["status"], "ready_for_parent_review")
        self.assertEqual(result["jev_calls"], 1)
        # Jev only classifies; the option set is fixed and generic.
        self.assertEqual(set(fake.calls[0][1]["i1:c1"]["criteria"]), OPTIONS)
        data, by_pair = self.decisions(result)
        decision = by_pair[("i1", "c1")]
        self.assertEqual(decision["match"], "exact")
        self.assertEqual(decision["status"], "covered")
        self.assertEqual(decision["confidence"], 0.98)
        self.assertEqual(set(decision["probabilities"]), OPTIONS)
        self.assertAlmostEqual(decision["probabilities"]["exact"], 0.99)
        self.assertEqual(decision["deterministic_flags"], OK_FLAGS)
        self.assertEqual(data["exceptions"], [])

    def test_partial_becomes_exception(self):
        self.start()
        result = rt.advance(self.run, client=FilterJev({"i1:c1": "partial"}))
        self.assertEqual(result["status"], "ready_for_parent_review")
        data, by_pair = self.decisions(result)
        self.assertEqual(by_pair[("i1", "c1")]["status"], "exception")
        self.assertEqual(by_pair[("i1", "c1")]["reason"], "partial")
        self.assertEqual([e["candidate_id"] for e in data["exceptions"]], ["c1"])

    def test_none_becomes_missing_and_stays_an_exception(self):
        self.start()
        result = rt.advance(self.run, client=FilterJev({"i1:c1": "none"}))
        self.assertEqual(result["status"], "ready_for_parent_review")
        data, by_pair = self.decisions(result)
        self.assertEqual(by_pair[("i1", "c1")]["match"], "none")
        self.assertEqual(by_pair[("i1", "c1")]["status"], "missing")
        self.assertEqual([e["reason"] for e in data["exceptions"]], ["missing"])

    def test_deterministic_failure_cannot_become_covered(self):
        self.write_artifact(("i1", "c1", BAD_FLAGS))
        self.start()
        result = rt.advance(self.run, client=FilterJev({"i1:c1": "exact"}))
        self.assertEqual(result["status"], "ready_for_parent_review")
        data, by_pair = self.decisions(result)
        self.assertEqual(by_pair[("i1", "c1")]["match"], "exact")
        self.assertEqual(by_pair[("i1", "c1")]["status"], "exception")
        self.assertEqual(by_pair[("i1", "c1")]["reason"], "deterministic_failed")
        self.assertEqual(len(data["exceptions"]), 1)

    def test_low_confidence_exact_escalates_instead_of_covering(self):
        self.start()
        fake = FilterJev({"i1:c1": "exact"}, confidences={"i1:c1": 0.10})
        result = rt.advance(self.run, client=fake)
        self.assertEqual(result["status"], "ready_for_parent_review")
        data, by_pair = self.decisions(result)
        self.assertEqual(by_pair[("i1", "c1")]["status"], "exception")
        self.assertEqual(by_pair[("i1", "c1")]["reason"], "low_confidence")
        self.assertEqual(len(data["exceptions"]), 1)

    # --- boundaries ------------------------------------------------------

    def test_filter_requires_explicit_typesafe_permission(self):
        self.start(allow=False)
        fake = FilterJev({"i1:c1": "exact"})
        result = rt.advance(self.run, client=fake)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason"], "typesafe_not_authorized")
        self.assertEqual(len(fake.calls), 0)
        self.assertNotIn("filter", result["completed"])

    def test_pairs_are_batched_by_batch_size(self):
        self.write_artifact(("i1", "c1", OK_FLAGS), ("i1", "c2", OK_FLAGS), ("i2", "c3", OK_FLAGS))
        self.plan["limits"]["max_jev_calls"] = 2  # exactly ceil(3 / 2)
        self.start()
        fake = FilterJev({"i1:c1": "exact", "i1:c2": "partial", "i2:c3": "none"})
        result = rt.advance(self.run, client=fake)
        self.assertEqual(result["status"], "ready_for_parent_review")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual([len(call[1]) for call in fake.calls], [2, 1])
        self.assertEqual(result["jev_calls"], 2)
        data, by_pair = self.decisions(result)
        self.assertEqual({key: value["status"] for key, value in by_pair.items()},
                         {("i1", "c1"): "covered", ("i1", "c2"): "exception", ("i2", "c3"): "missing"})
        item_decisions = {row["item_id"]: row for row in data["item_decisions"]}
        self.assertEqual(set(item_decisions), {"i1", "i2"})
        self.assertEqual(item_decisions["i1"]["candidate_id"], "c1")
        self.assertEqual(item_decisions["i1"]["status"], "covered")
        self.assertEqual(item_decisions["i2"]["candidate_id"], "c3")
        self.assertEqual(item_decisions["i2"]["status"], "missing")
        # Rejected alternatives stay in pair evidence, but do not become owner exceptions.
        self.assertEqual(data["exceptions"], [
            {"item_id": "i2", "candidate_id": "c3", "reason": "missing"}])

    def test_budget_below_required_batches_stops_without_covering(self):
        self.write_artifact(("i1", "c1", OK_FLAGS), ("i1", "c2", OK_FLAGS), ("i2", "c3", OK_FLAGS))
        self.plan["limits"]["max_jev_calls"] = 1  # one short of ceil(3 / 2)
        self.start()
        fake = FilterJev({"i1:c1": "exact", "i1:c2": "exact", "i2:c3": "exact"})
        result = rt.advance(self.run, client=fake)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason"], "jev_budget_exhausted")
        self.assertEqual(len(fake.calls), 1)
        self.assertNotIn("filter", result["completed"])

    def test_malformed_response_fails_closed(self):
        self.start()
        result = rt.advance(self.run, client=FilterJev({"i1:c1": "exact"}, corrupt=True))
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason"], "jev_unavailable")
        self.assertNotIn("filter", result["completed"])
        self.assertFalse((self.run / "artifacts" / "filter" / "decisions.json").exists())


if __name__ == "__main__":
    unittest.main()
