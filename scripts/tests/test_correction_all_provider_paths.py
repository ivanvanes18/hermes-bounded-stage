"""Public-boundary regressions: every probabilistic dispatch requires parent admission."""
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import executor_runtime as E
import executor_routes as R
import outbound_fixtures as OF
import routing_fixtures as F
from schema_validation import digest


class ChoiceRecorder:
    def __init__(self, preferred=None):
        self.preferred = preferred
        self.payloads = []

    def evaluate(self, state, questions):
        self.payloads.append({"state": state, "questions": questions})
        answers = {}
        for name, question in questions.items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.01}
                continue
            options = list(question["criteria"])
            choice = self.preferred if self.preferred in options else options[0]
            answers[name] = {
                "type": "choice",
                "choice": choice,
                "probabilities": {item: 1.0 if item == choice else 0.0 for item in options},
                "confidence": 1.0,
            }
        return {
            "model": "jev-1.13.0",
            "answers": answers,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


class AllProviderAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stage = F.stage(self.root)
        self.registry = F.registry()
        F.attach_adapter(self.root, self.registry)
        self.run_dir = self.root / "run"

    def readiness(self):
        route = next(item for item in self.registry["routes"] if item["route_id"] == "fixture_worker")
        return {
            "fixture_worker": {
                "ready": True,
                "route_hash": digest(route),
                "registry_hash": digest(self.registry),
                "stage_hash": digest(self.stage),
                "evidence_kind": "synthetic",
                "harness": route["harness"],
                "model": route["model"],
                "effort": route["effort"],
                "supported_modes": ["bounded_write"],
                "adapter_digest": digest(route["adapter"]),
                "reason": "ready",
                "expires_at": time.time() + 300,
            }
        }

    def test_executor_routing_without_admission_makes_zero_provider_calls(self):
        E.initialize(self.stage, self.registry, self.run_dir, evidence_mode="synthetic")
        recorder = ChoiceRecorder("owner")
        readiness = self.readiness()
        with patch.object(E.harness, "readiness", return_value=readiness):
            result = E.advance(self.run_dir, providers={"executor_routing": recorder})
        self.assertEqual(recorder.payloads, [])
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason"], "provider_unavailable")

    def test_executor_routing_exact_admission_classifies_full_local_context(self):
        marker = "LOCAL_CONTEXT_OUTSIDE_ROUTING_PAYLOAD"
        source = self.root / "routing-context.txt"
        source.write_text(marker + "\n")
        self.stage["context"] = {
            "candidates": [{"id": "local", "source_path": str(source), "source_sha256": F.sha(source),
                            "start_line": 1, "end_line": 1, "mandatory": False}],
            "mandatory_ids": [],
            "top_k": 1,
        }
        E.initialize(self.stage, self.registry, self.run_dir, evidence_mode="synthetic")
        readiness = self.readiness()
        request = R.request(self.stage, self.registry, readiness)
        observed = []
        def classifier(raw):
            observed.append(raw)
            return "synthetic"
        policy = OF.admission([(request["state"], request["questions"])], "executor_routing", classifier=classifier)
        recorder = ChoiceRecorder("owner")
        with patch.object(E.harness, "readiness", return_value=readiness), \
             patch("outbound_admission.canonical_redact", OF.redact):
            result = E.advance(self.run_dir, providers={"executor_routing": recorder},
                               admissions={"executor_routing": policy})
        self.assertEqual(len(recorder.payloads), 1)
        self.assertEqual(result["status"], "needs_review")
        self.assertIn(marker, str(observed))

    def test_executor_routing_secret_classification_makes_zero_provider_calls(self):
        self.stage["objective"] = "Do not send " + OF.CANARY
        E.initialize(self.stage, self.registry, self.run_dir, evidence_mode="synthetic")
        readiness = self.readiness()
        request = R.request(self.stage, self.registry, readiness)
        policy = OF.admission([(request["state"], request["questions"])], "executor_routing")
        recorder = ChoiceRecorder("owner")
        with patch.object(E.harness, "readiness", return_value=readiness), \
             patch("outbound_admission.canonical_redact", OF.redact):
            result = E.advance(self.run_dir, providers={"executor_routing": recorder},
                               admissions={"executor_routing": policy})
        self.assertEqual(recorder.payloads, [])
        self.assertEqual(result["status"], "needs_review")

    def test_transition_without_admissions_makes_zero_provider_calls(self):
        self.stage["features"]["stage_transition"] = True
        E.initialize(self.stage, self.registry, self.run_dir, evidence_mode="synthetic")
        first = E.advance(self.run_dir, fixed_route="fixture_worker")
        self.assertEqual(first["status"], "ready_for_parent_review")
        triage = ChoiceRecorder("implementation_defect")
        transition = ChoiceRecorder("complete")
        result = E.transition(
            self.run_dir,
            providers={"triage": triage, "stage_transition": transition},
        )
        self.assertEqual(triage.payloads, [])
        self.assertEqual(transition.payloads, [])
        self.assertEqual(result["last_transition"]["transition"], "return_to_owner")

    def test_transition_exact_admissions_call_each_purpose_once(self):
        self.stage["features"]["stage_transition"] = True
        E.initialize(self.stage, self.registry, self.run_dir, evidence_mode="synthetic")
        first = E.advance(self.run_dir, fixed_route="fixture_worker")
        facts = {
            "deterministic_passed": True,
            "expected_red": False,
            "environment_failure": False,
            "missing_dependency": False,
            "missing_evidence": False,
            "scope_mismatch": False,
            "stale_plan": False,
        }
        triage_questions = {"triage": {"type": "choice",
            "instructions": "Classify this failure without proposing or performing repairs.",
            "criteria": {name: name.replace("_", " ") for name in (
                "implementation_defect", "test_defect", "environment_failure", "missing_dependency",
                "missing_evidence", "scope_mismatch", "stale_plan", "expected_red", "unknown")}}}
        triage_policy = OF.admission([({"controller_facts": facts}, triage_questions)], "triage")
        ctx = {
            "checks_passed": True,
            "correction_used": False,
            "parent_authorized_correction": False,
            "evidence_hash": first["result_hash"],
            "previous_evidence_hash": None,
            "scope_violations": [],
            "triage_category": "implementation_defect",
            "remaining_worker_calls": 1,
            "hard_owner_boundary": False,
        }
        transition_questions = {"next_stage": {"type": "choice",
            "instructions": "Choose only the next whole stage from the controller whitelist. Do not plan or execute micro-actions.",
            "criteria": {name: name.replace("_", " ") for name in
                         ("complete", "return_to_owner", "clarify", "stop")}}}
        transition_policy = OF.admission([({"controller_context": ctx}, transition_questions)], "stage_transition")
        triage = ChoiceRecorder("implementation_defect")
        transition = ChoiceRecorder("complete")
        with patch("outbound_admission.canonical_redact", OF.redact):
            result = E.transition(
                self.run_dir,
                providers={"triage": triage, "stage_transition": transition},
                admissions={"triage": triage_policy, "stage_transition": transition_policy},
            )
        self.assertEqual(len(triage.payloads), 1)
        self.assertEqual(len(transition.payloads), 1)
        self.assertEqual(result["last_transition"]["transition"], "complete")

    def test_triage_workspace_mutation_blocks_stage_transition_provider(self):
        self.stage["features"]["stage_transition"] = True
        E.initialize(self.stage, self.registry, self.run_dir, evidence_mode="synthetic")
        first = E.advance(self.run_dir, fixed_route="fixture_worker")
        facts = {"deterministic_passed": True, "expected_red": False, "environment_failure": False,
                 "missing_dependency": False, "missing_evidence": False, "scope_mismatch": False,
                 "stale_plan": False}
        triage_questions = {"triage": {"type": "choice",
            "instructions": "Classify this failure without proposing or performing repairs.",
            "criteria": {name: name.replace("_", " ") for name in (
                "implementation_defect", "test_defect", "environment_failure", "missing_dependency",
                "missing_evidence", "scope_mismatch", "stale_plan", "expected_red", "unknown")}}}
        triage_policy = OF.admission([({"controller_facts": facts}, triage_questions)], "triage")
        ctx = {"checks_passed": True, "correction_used": False, "parent_authorized_correction": False,
               "evidence_hash": first["result_hash"], "previous_evidence_hash": None,
               "scope_violations": [], "triage_category": "unknown", "remaining_worker_calls": 1,
               "hard_owner_boundary": False}
        transition_questions = {"next_stage": {"type": "choice",
            "instructions": "Choose only the next whole stage from the controller whitelist. Do not plan or execute micro-actions.",
            "criteria": {name: name.replace("_", " ") for name in
                         ("complete", "return_to_owner", "clarify", "stop")}}}
        transition_policy = OF.admission([({"controller_context": ctx}, transition_questions)], "stage_transition")
        target = Path(self.stage["workspace"]) / "sample.py"
        class MutatingTriage(ChoiceRecorder):
            def evaluate(inner, state, questions):
                target.write_text("mutated during triage")
                return super(MutatingTriage, inner).evaluate(state, questions)
        triage = MutatingTriage("implementation_defect")
        transition = ChoiceRecorder("complete")
        with patch("outbound_admission.canonical_redact", OF.redact):
            result = E.transition(
                self.run_dir,
                providers={"triage": triage, "stage_transition": transition},
                admissions={"triage": triage_policy, "stage_transition": transition_policy},
            )
        self.assertEqual(len(triage.payloads), 1)
        self.assertEqual(transition.payloads, [])
        self.assertEqual(result["last_transition"]["transition"], "return_to_owner")


if __name__ == "__main__":
    unittest.main()
