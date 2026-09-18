#!/usr/bin/env python3
"""Hermes-facing entry point. All operational responses are bounded JSON."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

import bounded_runtime as runtime
from admission import EMPTY_CATALOG, validate_plan
from jev_client import JevClient, JevError, MODEL
from runtime_support import StageError, read_json, save_json, sha256_file


def demo(workspace: str, generation: bool = False) -> dict:
    base = Path(workspace).expanduser().resolve()
    if not base.is_dir():
        raise StageError("workspace_missing")
    folder = base / ("bounded-stage-demo-" + uuid.uuid4().hex[:10])
    folder.mkdir(mode=0o700)
    source = folder / "source.json"
    save_json(source, {"operation": "Поставка", "name": "Клапан X1", "quantity": "12", "unit": "шт."})
    first = {"id": "first", "output": "copy.json", "context": ["source"],
             "actions": [{"id": "copy", "kind": "copy", "source": "source", "description": "Copy exactly"}],
             "checks": [{"kind": "json"}, {"kind": "same_sha256", "source": "source"}], "semantic_checks": []}
    if generation:
        first["actions"] = [{"id": "describe", "kind": "generate", "description": "Add a short explanatory note",
                             "instructions": "Верни JSON: сохрани исходные поля operation, name, quantity, unit без изменений и добавь поле note с коротким описанием позиции. Только JSON, без ограждения кода."}]
        first["checks"] = [{"kind": "json"}, {"kind": "json_has_keys", "fields": ["note"]},
                           {"kind": "json_fields_equal", "source": "source", "fields": ["operation", "name", "quantity", "unit"]}]
    second = {"id": "second", "output": "checked.json", "context": ["first"],
              "actions": [{"id": "copy", "kind": "copy", "source": "first", "description": "Copy checked intermediate"}],
              "checks": [{"kind": "json_fields_equal", "source": "first", "fields": ["operation", "name", "quantity", "unit"]}],
              "semantic_checks": []}
    plan = {"schema_version": 1, "stage_id": "offline-demo", "goal": "Exercise two deterministic steps, not a real estimate",
            "constraints": ["Preserve the supplied synthetic record exactly"],
            "inputs": {"source": {"path": str(source), "sha256": sha256_file(source)}},
            "steps": [first, second], "limits": {"max_jev_calls": 0, "max_generations": 1 if generation else 0, "max_seconds": 900}}
    plan_path = folder / "plan.json"; save_json(plan_path, plan)
    runtime.initialize(plan_path, folder / "run")
    result = runtime.advance(folder / "run")
    result["demo_scope"] = "offline controller only; no Hermes or model calls"
    result["demo_plan"] = str(plan_path)
    return result


def main(argv=None):
    effective = list(sys.argv[1:] if argv is None else argv)
    if effective and effective[0].startswith("route-"):
        from route_cli import main as route_main
        return route_main(effective)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Local prerequisites only; no secrets or network requests")
    hasher = commands.add_parser("hash", help="Hash one bounded regular file"); hasher.add_argument("path")
    validator = commands.add_parser("validate", help="Read-only plan/catalog/input validation")
    validator.add_argument("--plan", required=True); validator.add_argument("--catalog")
    init = commands.add_parser("init", help="Create a new sealed run; never overwrite")
    init.add_argument("--plan", required=True); init.add_argument("--run", required=True)
    init.add_argument("--catalog"); init.add_argument("--allow-typesafe", action="store_true")
    for name in ("advance", "inspect"):
        cmd = commands.add_parser(name); cmd.add_argument("--run", required=True)
    submit = commands.add_parser("submit", help="Consume the exact pending generation once")
    submit.add_argument("--run", required=True); submit.add_argument("--nonce", required=True)
    demonstration = commands.add_parser("demo", help="Run a two-step synthetic OFFLINE example")
    demonstration.add_argument("--workspace", required=True)
    demonstration.add_argument("--generation", action="store_true", help="Yield a synthetic text task to a real worker; no built-in model call")
    smoke = commands.add_parser("smoke", help="Explicit live TypeSafe call with synthetic non-client text")
    smoke.add_argument("--allow-typesafe", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = {"status": "local_prerequisites_checked", "python": sys.version.split()[0],
                      "python_supported": sys.version_info >= (3, 10), "platform": sys.platform,
                      "hermes_cli_present": shutil.which("hermes") is not None,
                      "typesafe_key_present": bool(os.environ.get("TYPESAFE_API_KEY")),
                      "network_checked": False, "hermes_runtime_verified": False,
                      "jev_model": MODEL, "required_native_tools_for_full_mode": ["terminal", "read_file", "write_file", "delegate_task"],
                      "configuration_changed": False}
        elif args.command == "hash":
            result = {"path": str(Path(args.path).expanduser().absolute()), "sha256": sha256_file(Path(args.path).expanduser())}
        elif args.command == "validate":
            plan = read_json(args.plan); catalog = read_json(args.catalog) if args.catalog else EMPTY_CATALOG
            validate_plan(plan, catalog); runtime._pins(catalog)
            for source in plan["inputs"].values():
                if sha256_file(source["path"]) != source["sha256"]:
                    raise StageError("input_hash_mismatch")
            result = {"status": "plan_valid", "stage_id": plan["stage_id"], "steps": len(plan["steps"]),
                      "plan_schema_version": plan["schema_version"],
                      "domain_methodology_approved": False}
        elif args.command == "init":
            result = runtime.initialize(args.plan, args.run, args.catalog, args.allow_typesafe)
        elif args.command == "advance":
            result = runtime.advance(args.run)
        elif args.command == "submit":
            result = runtime.submit(args.run, args.nonce)
        elif args.command == "inspect":
            result = runtime.inspect(args.run)
        elif args.command == "demo":
            result = demo(args.workspace, args.generation)
        elif args.command == "smoke":
            if not args.allow_typesafe:
                raise StageError("typesafe_not_authorized")
            questions = {"action": {"type": "choice", "instructions": "Какое действие явно запрошено в тексте? Не выполняй его.",
                                    "criteria": {"copy": "Скопировать файл без изменений", "generate": "Написать новый текст", "none": "Недостаточно данных"}}}
            response = JevClient().evaluate({"text": "Скопируй файл без изменения содержимого."}, questions)
            result = {"status": "live_smoke_response", "response": response, "calibrated": False,
                      "expected_choice_observed": response["answers"]["action"]["choice"] == "copy"}
        else:
            raise StageError("unknown_command")
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return {"awaiting_generation": 10, "needs_review": 20}.get(result.get("status"), 0)
    except (StageError, JevError) as exc:
        reason = "jev_unavailable" if isinstance(exc, JevError) else str(exc)
        print(json.dumps({"status": "blocked", "reason": reason}, ensure_ascii=False))
        return 2
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        print(json.dumps({"status": "blocked", "reason": "invalid_runtime_or_contract"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
