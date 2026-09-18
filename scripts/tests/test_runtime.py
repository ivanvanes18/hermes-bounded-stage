"""Offline contract tests. Fake provider replies are NOT live model evidence."""
import copy
import hashlib
import json
import math
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
    import bounded_runtime as rt
    import jev_client as jc
except ImportError:
    rt = jc = None


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class FakeJev:
    def __init__(self, choices=None, confidence=0.98, probability=0.99, error=None):
        self.choices = choices or {}
        self.confidence = confidence
        self.probability = probability
        self.error = error
        self.calls = []

    def evaluate(self, state, questions):
        self.calls.append((copy.deepcopy(state), copy.deepcopy(questions)))
        if self.error:
            raise self.error
        answers = {}
        for name, q in questions.items():
            chosen = self.choices.get(name, next(iter(q["criteria"])))
            keys = list(q["criteria"])
            p = {k: (1 - self.probability) / (len(keys) - 1) for k in keys}
            p[chosen] = self.probability
            answers[name] = {"type": "choice", "choice": chosen,
                             "confidence": self.confidence, "probabilities": p}
        return {"model": "jev-1.13.0", "answers": answers,
                "usage": {"input_tokens": 100, "output_tokens": 10}}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(rt, "bounded_runtime implementation is missing")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "исходник.txt"
        self.source.write_text("Поставка клапана X1 — 12 шт.\n", encoding="utf-8")
        self.plan = {
            "schema_version": 1, "stage_id": "sample", "goal": "Prepare a checked copy",
            "constraints": ["Do not change quantities or units"],
            "inputs": {"source": {"path": str(self.source), "sha256": digest(self.source)}},
            "steps": [{"id": "first", "output": "результат.txt", "context": ["source"],
                       "actions": [{"id": "keep", "kind": "copy", "source": "source", "description": "Keep exactly"}],
                       "checks": [{"kind": "same_sha256", "source": "source"}],
                       "semantic_checks": []}],
            "limits": {"max_jev_calls": 8, "max_generations": 2, "max_seconds": 600}}
        self.plan_path = self.root / "plan.json"
        self.run = self.root / "run"

    def start(self, allow=False, registry=None):
        dump(self.plan_path, self.plan)
        regpath = None
        if registry is not None:
            regpath = self.root / "catalog.json"
            dump(regpath, registry)
        return rt.initialize(self.plan_path, self.run, registry_path=regpath, allow_typesafe=allow)

    def state(self):
        return json.loads((self.run / "state.json").read_text())

    def generated(self):
        self.plan["steps"][0]["actions"] = [{"id": "rewrite", "kind": "generate", "description": "Rewrite",
                                                "instructions": "Keep quantity, unit, and identity; only tidy wording."}]
        self.plan["steps"][0]["checks"] = [{"kind": "nonempty"}, {"kind": "same_numeric_tokens", "source": "source"}]
        self.start()
        state = rt.advance(self.run)
        self.assertEqual(state["status"], "awaiting_generation")
        return state

    def add_choice(self):
        step = self.plan["steps"][0]
        step["actions"].append({"id": "other", "kind": "copy", "source": "source", "description": "Alternative unchanged copy"})
        step["selection"] = {"instructions": "Select keep only when preserving exact source is appropriate."}

    def add_semantic(self):
        self.plan["steps"][0]["semantic_checks"] = [{"id": "meaning", "instructions": "Does the output preserve the operation stated in source?"}]

    def test_two_steps_run_without_model_calls(self):
        second = copy.deepcopy(self.plan["steps"][0]); second["id"] = "second"
        second["context"] = ["first"]; second["actions"][0]["source"] = "first"
        second["checks"][0]["source"] = "first"
        self.plan["steps"].append(second)
        self.start()
        result = rt.advance(self.run)
        self.assertEqual(result["status"], "ready_for_parent_review")
        self.assertEqual(len(result["completed"]), 2)
        self.assertEqual(result["jev_calls"], 0)
        self.assertEqual(result["generations"], 0)
        self.assertEqual(result["final_acceptance"], "not_performed")

    def test_source_is_not_modified(self):
        before = self.source.read_bytes(); self.start(); rt.advance(self.run)
        self.assertEqual(self.source.read_bytes(), before)

    def test_input_hash_mismatch_refuses_creation(self):
        self.plan["inputs"]["source"]["sha256"] = "0" * 64
        with self.assertRaises(rt.StageError): self.start()
        self.assertFalse(self.run.exists())

    def test_existing_run_not_overwritten(self):
        self.start()
        with self.assertRaises(rt.StageError): rt.initialize(self.plan_path, self.run)

    def test_empty_checks_rejected(self):
        self.plan["steps"][0]["checks"] = []
        with self.assertRaises(rt.StageError): self.start()

    def test_unknown_plan_field_rejected(self):
        self.plan["ignore_checks"] = True
        with self.assertRaises(rt.StageError): self.start()

    def test_forward_reference_rejected(self):
        self.plan["steps"][0]["context"] = ["later"]
        with self.assertRaises(rt.StageError): self.start()

    def test_output_escape_rejected(self):
        self.plan["steps"][0]["output"] = "../outside"
        with self.assertRaises(rt.StageError): self.start()

    def test_duplicate_ids_rejected(self):
        self.plan["steps"].append(copy.deepcopy(self.plan["steps"][0]))
        with self.assertRaises(rt.StageError): self.start()

    def test_large_step_count_rejected(self):
        base = self.plan["steps"][0]
        self.plan["steps"] = [dict(copy.deepcopy(base), id=f"step{i}") for i in range(9)]
        with self.assertRaises(rt.StageError): self.start()

    def test_boolean_budget_rejected(self):
        self.plan["limits"]["max_generations"] = True
        with self.assertRaises(rt.StageError): self.start()

    def test_generation_yields_once(self):
        result = self.generated()
        repeated = rt.advance(self.run)
        self.assertEqual(repeated["pending"]["nonce"], result["pending"]["nonce"])
        self.assertEqual(repeated["generations"], 1)

    def test_generation_submit_then_validate(self):
        result = self.generated()
        Path(result["pending"]["submission_path"]).write_text("Поставка клапана X1: 12 шт.\n", encoding="utf-8")
        done = rt.submit(self.run, result["pending"]["nonce"])
        self.assertEqual(done["status"], "ready_for_parent_review")
        self.assertEqual(done["generations"], 1)

    def test_generation_cannot_change_number(self):
        result = self.generated()
        Path(result["pending"]["submission_path"]).write_text("Поставка клапана X1: 13 шт.", encoding="utf-8")
        done = rt.submit(self.run, result["pending"]["nonce"])
        self.assertEqual(done["status"], "needs_review")
        self.assertEqual(done["reason"], "check_failed:same_numeric_tokens")

    def test_wrong_nonce_cannot_submit(self):
        result = self.generated()
        Path(result["pending"]["submission_path"]).write_text("12 X1", encoding="utf-8")
        with self.assertRaises(rt.StageError): rt.submit(self.run, "wrong")
        self.assertEqual(self.state()["status"], "awaiting_generation")

    def test_replayed_submission_rejected(self):
        result = self.generated()
        Path(result["pending"]["submission_path"]).write_bytes(self.source.read_bytes())
        rt.submit(self.run, result["pending"]["nonce"])
        with self.assertRaises(rt.StageError): rt.submit(self.run, result["pending"]["nonce"])

    def test_generation_symlink_refused(self):
        result = self.generated()
        Path(result["pending"]["submission_path"]).symlink_to(self.source)
        done = rt.submit(self.run, result["pending"]["nonce"])
        self.assertEqual(done["status"], "needs_review")

    def test_source_drift_while_waiting(self):
        result = self.generated()
        Path(result["pending"]["submission_path"]).write_bytes(self.source.read_bytes())
        self.source.write_text("changed", encoding="utf-8")
        done = rt.submit(self.run, result["pending"]["nonce"])
        self.assertEqual(done["status"], "needs_review")
        self.assertIn("input_changed", done["reason"])

    def test_plan_tamper_refused(self):
        self.start(); sealed = json.loads((self.run / "plan.json").read_text())
        sealed["goal"] = "different"; dump(self.run / "plan.json", sealed)
        done = rt.advance(self.run)
        self.assertEqual(done["status"], "needs_review")
        self.assertEqual(done["reason"], "plan_changed")

    def test_interrupted_step_not_replayed(self):
        self.start(); state = self.state(); state["status"] = "executing"; dump(self.run / "state.json", state)
        done = rt.advance(self.run)
        self.assertEqual(done["status"], "needs_review")
        self.assertEqual(done["reason"], "interrupted_execution")

    def test_choice_requires_network_permission(self):
        self.add_choice(); self.start()
        fake = FakeJev(); result = rt.advance(self.run, client=fake)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason"], "typesafe_not_authorized")
        self.assertEqual(len(fake.calls), 0)

    def test_choice_of_allowed_action(self):
        self.add_choice(); self.start(allow=True)
        fake = FakeJev({"next_action": "keep"}); result = rt.advance(self.run, client=fake)
        self.assertEqual(result["status"], "ready_for_parent_review")
        self.assertIn("none", fake.calls[0][1]["next_action"]["criteria"])
        self.assertEqual(result["jev_calls"], 1)

    def test_none_choice_escalates(self):
        self.add_choice(); self.start(allow=True)
        result = rt.advance(self.run, client=FakeJev({"next_action": "none"}))
        self.assertEqual(result["reason"], "no_admissible_action")

    def test_low_confidence_escalates(self):
        self.add_choice(); self.start(allow=True)
        result = rt.advance(self.run, client=FakeJev({"next_action": "keep"}, confidence=0.1))
        self.assertEqual(result["reason"], "uncertain_choice")

    def test_model_error_never_means_pass(self):
        self.add_semantic(); self.start(allow=True)
        result = rt.advance(self.run, client=FakeJev(error=jc.JevError("unavailable")))
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason"], "jev_unavailable")
        self.assertEqual(result["jev_calls"], 1)

    def test_semantic_questions_use_real_source_and_output(self):
        self.add_semantic(); self.start(allow=True); fake = FakeJev({"meaning": "supported"})
        result = rt.advance(self.run, client=fake)
        self.assertEqual(result["status"], "ready_for_parent_review")
        sent = fake.calls[0][0]
        self.assertEqual(sent["sources"]["source"], self.source.read_text())
        self.assertEqual(sent["output"], self.source.read_text())

    def test_semantic_failure_escalates(self):
        self.add_semantic(); self.start(allow=True)
        result = rt.advance(self.run, client=FakeJev({"meaning": "contradicts"}))
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason"], "semantic_check_failed:meaning")

    def test_semantic_checks_batched(self):
        self.add_semantic(); self.plan["steps"][0]["semantic_checks"].append({"id": "second_check", "instructions": "Is identity preserved?"})
        self.start(allow=True); fake = FakeJev({"meaning": "supported", "second_check": "supported"})
        result = rt.advance(self.run, client=fake)
        self.assertEqual(result["status"], "ready_for_parent_review")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(fake.calls[0][1]), 2)

    def test_jev_budget_exhaustion(self):
        self.add_choice(); self.add_semantic(); self.plan["limits"]["max_jev_calls"] = 1
        self.start(allow=True); fake = FakeJev({"next_action": "keep"})
        result = rt.advance(self.run, client=fake)
        self.assertEqual(result["reason"], "jev_budget_exhausted")
        self.assertEqual(len(fake.calls), 1)

    def test_deadline_expired(self):
        self.start(); state = self.state(); state["deadline"] = 0; dump(self.run / "state.json", state)
        result = rt.advance(self.run)
        self.assertEqual(result["reason"], "deadline_exceeded")

    def test_completed_output_drift_is_visible(self):
        self.start(); done = rt.advance(self.run)
        Path(done["completed"]["first"]["path"]).write_text("corrupt", encoding="utf-8")
        inspected = rt.inspect(self.run)
        self.assertEqual(inspected["status"], "needs_review")
        self.assertIn("output_changed", inspected["reason"])

    def test_completed_output_symlink_still_reports_failure(self):
        self.start(); done = rt.advance(self.run)
        output = Path(done["completed"]["first"]["path"])
        output.unlink(); output.symlink_to(self.source)
        inspected = rt.inspect(self.run)
        self.assertEqual(inspected["status"], "needs_review")
        self.assertEqual(inspected["reason"], "symlink_not_allowed")

    def test_output_change_during_semantic_call_cannot_pass(self):
        self.add_semantic(); self.start(allow=True)
        output = self.run / "artifacts" / "first" / "результат.txt"
        class MutatingJev(FakeJev):
            def evaluate(inner, state, questions):
                reply = super().evaluate(state, questions)
                output.write_text("changed after deterministic checks", encoding="utf-8")
                return reply
        result = rt.advance(self.run, client=MutatingJev({"meaning": "supported"}))
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason"], "output_changed_during_validation")

    def test_incomplete_state_cannot_claim_finished(self):
        self.start(); state = self.state()
        state["status"] = "ready_for_parent_review"; state["next_index"] = 1
        dump(self.run / "state.json", state)
        result = rt.inspect(self.run)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason"], "state_progress_mismatch")

    def test_removed_input_record_cannot_hide_freshness_check(self):
        self.start(); state = self.state(); state["inputs"] = {}; dump(self.run / "state.json", state)
        result = rt.advance(self.run)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason"], "state_input_mismatch")

    def test_generation_budget_zero(self):
        self.plan["steps"][0]["actions"] = [{"id": "gen", "kind": "generate", "description": "Text", "instructions": "Keep source"}]
        self.plan["limits"]["max_generations"] = 0; self.start()
        result = rt.advance(self.run)
        self.assertEqual(result["reason"], "generation_budget_exhausted")
        self.assertEqual(result["generations"], 0)

    def test_bad_validator_stops_before_semantic_call(self):
        self.add_semantic(); pending = self.generated()
        Path(pending["pending"]["submission_path"]).write_text("X1 13 шт.", encoding="utf-8")
        fake = FakeJev({"meaning": "supported"})
        result = rt.submit(self.run, pending["pending"]["nonce"], client=fake)
        self.assertEqual(result["reason"], "check_failed:same_numeric_tokens")
        self.assertEqual(len(fake.calls), 0)

    def test_network_flag_must_be_boolean(self):
        dump(self.plan_path, self.plan)
        with self.assertRaises(rt.StageError):
            rt.initialize(self.plan_path, self.run, allow_typesafe="false")

    def test_inspect_checks_pending_packet_integrity(self):
        pending = self.generated()
        packet_path = Path(pending["pending"]["packet_path"])
        packet = json.loads(packet_path.read_text()); packet["instructions"] = "Changed instructions"
        dump(packet_path, packet)
        result = rt.inspect(self.run)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason"], "generation_packet_changed")

    def test_command_cannot_publish_through_parent_symlink(self):
        catalog = self.command_registry("import pathlib,sys\nsrc=pathlib.Path(sys.argv[1]); out=pathlib.Path(sys.argv[2])\nout.parent.rmdir(); out.parent.symlink_to(src.parent, target_is_directory=True)\nout.write_bytes(src.read_bytes())\n")
        self.plan["steps"][0]["actions"] = [{"id": "convert", "kind": "command", "capability": "adapter", "description": "Test path boundary"}]
        self.start(registry=catalog)
        result = rt.advance(self.run)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason"], "symlink_not_allowed")

    def test_command_does_not_inherit_typesafe_key(self):
        catalog = self.command_registry("import pathlib,sys,os,json\npathlib.Path(sys.argv[2]).write_text(json.dumps({'key_present': bool(os.environ.get('TYPESAFE_API_KEY'))}))\n")
        self.plan["steps"][0]["actions"] = [{"id": "convert", "kind": "command", "capability": "adapter", "description": "Check environment isolation"}]
        self.plan["steps"][0]["checks"] = [{"kind": "json"}]
        self.start(registry=catalog)
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "TEST_ONLY_NOT_A_REAL_KEY"}):
            result = rt.advance(self.run)
        self.assertEqual(result["status"], "ready_for_parent_review")
        self.assertFalse(json.loads(Path(result["completed"]["first"]["path"]).read_text())["key_present"])

    def command_registry(self, text, role="action", timeout=5):
        script = self.root / "adapter.py"; script.write_text(text, encoding="utf-8")
        exe = str(Path(sys.executable).resolve())
        return {"schema_version": 1, "capabilities": {"adapter": {
            "role": role, "argv": [exe, str(script), "{ref:source}", "{output}"],
            "pins": {exe: digest(exe), str(script): digest(script)}, "timeout_seconds": timeout}}}

    def test_registered_command_runs(self):
        catalog = self.command_registry("import pathlib,sys\npathlib.Path(sys.argv[2]).write_bytes(pathlib.Path(sys.argv[1]).read_bytes())\n")
        self.plan["steps"][0]["actions"] = [{"id": "convert", "kind": "command", "capability": "adapter", "description": "Registered copier"}]
        self.start(registry=catalog)
        self.assertEqual(rt.advance(self.run)["status"], "ready_for_parent_review")

    def test_command_timeout_has_no_retry(self):
        catalog = self.command_registry("import time\ntime.sleep(3)\n", timeout=1)
        self.plan["steps"][0]["actions"] = [{"id": "convert", "kind": "command", "capability": "adapter", "description": "Test timeout"}]
        self.start(registry=catalog)
        result = rt.advance(self.run)
        self.assertEqual(result["reason"], "command_timeout")
        self.assertEqual(rt.advance(self.run)["status"], "needs_review")

    def test_modified_adapter_is_not_executed(self):
        catalog = self.command_registry("pass\n")
        self.plan["steps"][0]["actions"] = [{"id": "convert", "kind": "command", "capability": "adapter", "description": "Registered adapter"}]
        self.start(registry=catalog); (self.root / "adapter.py").write_text("print('changed')")
        self.assertEqual(rt.advance(self.run)["reason"], "capability_changed")

    def test_validator_must_not_change_output(self):
        catalog = self.command_registry("import pathlib,sys\npathlib.Path(sys.argv[2]).write_text('bad')\n", role="check")
        self.plan["steps"][0]["checks"].append({"kind": "command", "capability": "adapter"})
        self.start(registry=catalog)
        self.assertEqual(rt.advance(self.run)["reason"], "validator_modified_output")

    def test_missing_command_capability_rejected(self):
        self.plan["steps"][0]["actions"] = [{"id": "convert", "kind": "command", "capability": "absent", "description": "No adapter"}]
        with self.assertRaises(rt.StageError): self.start()


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(jc, "jev_client implementation is missing")
        self.questions = {"pick": {"type": "choice", "instructions": "Pick", "criteria": {"a": "A", "none": "None"}}}
        self.reply = FakeJev({"pick": "a"}).evaluate({}, self.questions)

    def test_client_missing_key_never_starts_http(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(jc.subprocess, "run") as call:
            with self.assertRaises(jc.JevError): jc.JevClient().evaluate({}, self.questions)
            call.assert_not_called()

    def test_client_request_shape_and_key_boundary(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(self.reply).encode())
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "TEST_ONLY_NOT_A_REAL_KEY"}), patch.object(jc.subprocess, "run", return_value=completed) as call:
            reply = jc.JevClient().evaluate({"text": "synthetic"}, self.questions)
        self.assertEqual(reply["model"], "jev-1.13.0")
        self.assertEqual(call.call_count, 1)
        sent = json.loads(call.call_args.kwargs["input"])
        self.assertEqual(sent["model"], "jev-1.13.0")
        self.assertEqual(sent["questions"], self.questions)
        self.assertNotIn("TEST_ONLY_NOT_A_REAL_KEY", repr(call.call_args.args))
        self.assertEqual(set(call.call_args.kwargs["env"]), {"TYPESAFE_API_KEY", "PYTHONIOENCODING"})

    def test_http_timeout_is_one_attempt(self):
        failure = subprocess.TimeoutExpired(cmd="synthetic", timeout=1)
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "TEST_ONLY_NOT_A_REAL_KEY"}), patch.object(jc.subprocess, "run", side_effect=failure) as call:
            with self.assertRaises(jc.JevError): jc.JevClient().evaluate({}, self.questions)
        self.assertEqual(call.call_count, 1)

    def test_oversized_payload_never_starts_http(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "TEST_ONLY_NOT_A_REAL_KEY"}), patch.object(jc.subprocess, "run") as call:
            with self.assertRaises(jc.JevError): jc.JevClient().evaluate({"text": "x" * (jc.MAX_BODY + 1)}, self.questions)
            call.assert_not_called()

    def test_http_redirect_is_refused(self):
        with self.assertRaises(jc.JevError): jc._NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://example.invalid/")

    def test_good_response(self):
        answers = jc.validate_response(self.reply, self.questions)
        self.assertEqual(answers["pick"]["choice"], "a")

    def test_bad_distributions_rejected(self):
        for value in [float("nan"), float("inf"), True, -0.1, 1.1, "0.9"]:
            with self.subTest(value=value):
                reply = copy.deepcopy(self.reply); reply["answers"]["pick"]["probabilities"]["a"] = value
                with self.assertRaises(jc.JevError): jc.validate_response(reply, self.questions)

    def test_wrong_model_rejected(self):
        self.reply["model"] = "other"
        with self.assertRaises(jc.JevError): jc.validate_response(self.reply, self.questions)

    def test_missing_answer_rejected(self):
        self.reply["answers"] = {}
        with self.assertRaises(jc.JevError): jc.validate_response(self.reply, self.questions)

    def test_extra_answer_rejected(self):
        self.reply["answers"]["extra"] = self.reply["answers"]["pick"]
        with self.assertRaises(jc.JevError): jc.validate_response(self.reply, self.questions)

    def test_choice_must_be_argmax(self):
        self.reply["answers"]["pick"]["choice"] = "none"
        with self.assertRaises(jc.JevError): jc.validate_response(self.reply, self.questions)

    def test_unknown_option_rejected(self):
        self.reply["answers"]["pick"]["choice"] = "execute_shell"
        with self.assertRaises(jc.JevError): jc.validate_response(self.reply, self.questions)

    def test_wrong_answer_type_rejected(self):
        self.reply["answers"]["pick"]["type"] = "score"
        with self.assertRaises(jc.JevError): jc.validate_response(self.reply, self.questions)


if __name__ == "__main__":
    unittest.main()
