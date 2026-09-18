"""Strict, deliberately small stage and capability-catalog contracts."""
from __future__ import annotations

import math
from pathlib import Path
import re
from runtime_support import StageError

IDENT = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
HASH = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_THRESHOLDS = {"min_probability": 0.90, "min_confidence": 0.80, "min_margin": 0.20}
EMPTY_CATALOG = {"schema_version": 1, "capabilities": {}}
PLAN_VERSIONS = (1, 2)
CANDIDATE_VERSION = 2
DEFAULT_BATCH_SIZE = 8
MAX_BATCH_SIZE = 32
MAX_ITEMS = 256
MAX_CANDIDATES = 64
MAX_PAIRS = 512
MAX_FLAGS = 32


def obj(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise StageError("invalid_contract_fields")


def text(value, maximum=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise StageError("invalid_text")


def ident(value):
    if not isinstance(value, str) or not IDENT.fullmatch(value) or value == "none":
        raise StageError("invalid_identifier")


def sequence(value, minimum, maximum):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise StageError("invalid_list_length")


def threshold_numbers(value):
    for key in DEFAULT_THRESHOLDS:
        number = value.get(key, DEFAULT_THRESHOLDS[key])
        if type(number) not in (float, int) or not math.isfinite(number) or not 0 <= number <= 1:
            raise StageError("invalid_threshold")


def thresholds(value, with_id=False):
    req = ["instructions"] + (["id"] if with_id else [])
    obj(value, req, DEFAULT_THRESHOLDS)
    text(value["instructions"])
    if with_id:
        ident(value["id"])
    threshold_numbers(value)


def validate_catalog(catalog):
    obj(catalog, ["schema_version", "capabilities"])
    if type(catalog["schema_version"]) is not int or catalog["schema_version"] != 1:
        raise StageError("unsupported_catalog_version")
    caps = catalog["capabilities"]
    if not isinstance(caps, dict) or len(caps) > 16:
        raise StageError("invalid_catalog")
    for name, cap in caps.items():
        ident(name)
        obj(cap, ["role", "argv", "pins", "timeout_seconds"])
        if cap["role"] not in ("action", "check"):
            raise StageError("invalid_capability_role")
        sequence(cap["argv"], 1, 32)
        for token in cap["argv"]:
            text(token)
            if "{" in token or "}" in token:
                if token not in ("{output}", "{workdir}") and not re.fullmatch(r"\{ref:[a-z][a-z0-9_-]{0,47}\}", token):
                    raise StageError("invalid_argument_placeholder")
        exe = cap["argv"][0]
        if not Path(exe).is_absolute() or Path(exe).name.lower() in ("sh", "bash", "zsh", "fish", "dash", "cmd", "powershell", "pwsh"):
            raise StageError("shell_capability_not_allowed")
        if "-c" in cap["argv"]:
            raise StageError("inline_code_not_allowed")
        if "{output}" not in cap["argv"]:
            raise StageError("missing_output_argument")
        pins = cap["pins"]
        absolute = {arg for arg in cap["argv"] if Path(arg).is_absolute()}
        if not isinstance(pins, dict) or set(pins) != absolute:
            raise StageError("capability_pins_incomplete")
        if not all(isinstance(v, str) and HASH.fullmatch(v) for v in pins.values()):
            raise StageError("invalid_pin")
        if type(cap["timeout_seconds"]) is not int or not 1 <= cap["timeout_seconds"] <= 120:
            raise StageError("invalid_command_timeout")


def _reference(ref, context):
    if ref not in context:
        raise StageError("reference_not_in_context")


def _capability(name, role, catalog, context):
    if not isinstance(name, str) or name not in catalog["capabilities"]:
        raise StageError("unknown_capability")
    cap = catalog["capabilities"][name]
    if cap["role"] != role:
        raise StageError("capability_role_mismatch")
    for token in cap["argv"]:
        if token.startswith("{ref:"):
            _reference(token[5:-1], context)


def _filter_action(action, step):
    """Generic semantic filter: it classifies supplied pairs and creates nothing."""
    _reference(action["source"], step["context"])
    text(action["instructions"])
    flags = action.get("required_flags", [])
    sequence(flags, 0, MAX_FLAGS)
    for name in flags:
        ident(name)
    if len(set(flags)) != len(flags):
        raise StageError("duplicate_required_flag")
    size = action.get("batch_size", DEFAULT_BATCH_SIZE)
    if type(size) is not int or not 1 <= size <= MAX_BATCH_SIZE:
        raise StageError("invalid_batch_size")
    threshold_numbers(action)


def validate_candidates(data, required_flags=()):
    """Closed artifact from an admitted producer; returns the ordered pairs to classify.

    Deterministic flags are supplied evidence, never something Jev may compute.
    """
    obj(data, ["schema_version", "items"])
    if type(data["schema_version"]) is not int or data["schema_version"] != CANDIDATE_VERSION:
        raise StageError("unsupported_candidate_version")
    sequence(data["items"], 1, MAX_ITEMS)
    pairs = []
    items = set()
    for item in data["items"]:
        obj(item, ["item_id", "text", "candidates"])
        ident(item["item_id"]); text(item["text"])
        if item["item_id"] in items:
            raise StageError("duplicate_item_id")
        items.add(item["item_id"])
        sequence(item["candidates"], 1, MAX_CANDIDATES)
        candidates = set()
        for candidate in item["candidates"]:
            obj(candidate, ["candidate_id", "text", "flags"])
            ident(candidate["candidate_id"]); text(candidate["text"])
            if candidate["candidate_id"] in candidates:
                raise StageError("duplicate_candidate_id")
            candidates.add(candidate["candidate_id"])
            flags = candidate["flags"]
            if not isinstance(flags, dict) or len(flags) > MAX_FLAGS:
                raise StageError("invalid_deterministic_flags")
            for name, value in flags.items():
                ident(name)
                if type(value) is not bool:
                    raise StageError("invalid_deterministic_flags")
            if not set(required_flags) <= set(flags):
                raise StageError("missing_deterministic_flag")
            if len(pairs) >= MAX_PAIRS:
                raise StageError("too_many_candidate_pairs")
            pairs.append({"key": item["item_id"] + ":" + candidate["candidate_id"],
                          "item_id": item["item_id"], "item_text": item["text"],
                          "candidate_id": candidate["candidate_id"],
                          "candidate_text": candidate["text"], "flags": flags})
    return pairs


def validate_plan(plan, catalog=None):
    catalog = EMPTY_CATALOG if catalog is None else catalog
    validate_catalog(catalog)
    obj(plan, ["schema_version", "stage_id", "goal", "constraints", "inputs", "steps", "limits"])
    if type(plan["schema_version"]) is not int or plan["schema_version"] not in PLAN_VERSIONS:
        raise StageError("unsupported_plan_version")
    ident(plan["stage_id"]); text(plan["goal"])
    sequence(plan["constraints"], 0, 16)
    for value in plan["constraints"]:
        text(value, 1000)
    inputs = plan["inputs"]
    if not isinstance(inputs, dict) or not 1 <= len(inputs) <= 8:
        raise StageError("invalid_inputs")
    for name, entry in inputs.items():
        ident(name); obj(entry, ["path", "sha256"]); text(entry["path"])
        if not Path(entry["path"]).is_absolute() or not isinstance(entry["sha256"], str) or not HASH.fullmatch(entry["sha256"]):
            raise StageError("invalid_input_binding")
    limits = plan["limits"]
    obj(limits, ["max_jev_calls", "max_generations", "max_seconds"])
    for key, minimum, maximum in (("max_jev_calls", 0, 32), ("max_generations", 0, 8), ("max_seconds", 1, 3600)):
        if type(limits[key]) is not int or not minimum <= limits[key] <= maximum:
            raise StageError("invalid_budget")
    sequence(plan["steps"], 1, 8)
    available = set(inputs)
    for step in plan["steps"]:
        obj(step, ["id", "output", "context", "actions", "checks", "semantic_checks"], ["selection"])
        ident(step["id"])
        if step["id"] in available:
            raise StageError("duplicate_identifier")
        name = step["output"]; text(name, 120)
        if name.startswith(".") or "/" in name or "\\" in name or any(ord(c) < 32 for c in name):
            raise StageError("invalid_output_name")
        sequence(step["context"], 1, 8)
        if not all(isinstance(x, str) and x in available for x in step["context"]) or len(set(step["context"])) != len(step["context"]):
            raise StageError("invalid_context_reference")
        sequence(step["actions"], 1, 8)
        ids = set()
        for action in step["actions"]:
            kind = action.get("kind") if isinstance(action, dict) else None
            extra = {"copy": "source", "generate": "instructions", "command": "capability",
                     "semantic_exception_filter": "source"}.get(kind)
            # The only v2 addition; a v1 card keeps exactly the v1 set of kinds.
            if extra is None or (kind == "semantic_exception_filter" and plan["schema_version"] < 2):
                raise StageError("unknown_action_kind")
            if kind == "semantic_exception_filter":
                obj(action, ["id", "kind", "description", "source", "instructions"],
                    ["required_flags", "batch_size"] + list(DEFAULT_THRESHOLDS))
            else:
                obj(action, ["id", "kind", "description", extra])
            ident(action["id"]); text(action["description"], 1000)
            if action["id"] in ids:
                raise StageError("duplicate_action")
            ids.add(action["id"])
            if kind == "copy":
                _reference(action["source"], step["context"])
            elif kind == "generate":
                text(action["instructions"])
            elif kind == "semantic_exception_filter":
                _filter_action(action, step)
            else:
                _capability(action["capability"], "action", catalog, step["context"])
        if len(step["actions"]) > 1:
            if "selection" not in step:
                raise StageError("missing_selection_contract")
            thresholds(step["selection"])
        elif "selection" in step:
            raise StageError("unnecessary_selection")
        sequence(step["checks"], 1, 16)
        for check in step["checks"]:
            if not isinstance(check, dict):
                raise StageError("invalid_check")
            kind = check.get("kind")
            if kind in ("nonempty", "json"):
                obj(check, ["kind"])
            elif kind in ("same_sha256", "same_numeric_tokens"):
                obj(check, ["kind", "source"]); _reference(check["source"], step["context"])
            elif kind in ("json_has_keys", "json_fields_equal"):
                obj(check, ["kind", "fields"] + (["source"] if kind == "json_fields_equal" else []))
                sequence(check["fields"], 1, 32)
                for field in check["fields"]:
                    text(field, 100)
                if kind == "json_fields_equal":
                    _reference(check["source"], step["context"])
            elif kind == "command":
                obj(check, ["kind", "capability"]); _capability(check["capability"], "check", catalog, step["context"])
            else:
                raise StageError("unknown_check")
        sequence(step["semantic_checks"], 0, 8)
        semantic_ids = set()
        for check in step["semantic_checks"]:
            thresholds(check, with_id=True)
            if check["id"] in semantic_ids:
                raise StageError("duplicate_semantic_check")
            semantic_ids.add(check["id"])
        available.add(step["id"])
    return plan
