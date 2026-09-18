# Hermes Bounded Stage (`/bs`)

A bounded-stage controller skill for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

It separates responsibilities between:

- the parent model, which owns the task, plan, permissions, and final acceptance;
- a deterministic Python controller, which owns sequencing, admission checks, budgets, hashes, nonces, and verification gates;
- an optional worker or Jev/TypeSafe route, which receives only the current bounded stage.

The skill is fail-closed: uncertainty, drift, invalid contracts, missing permissions, failed checks, or unavailable providers return control to the owner instead of being treated as success.

## Install

```bash
git clone https://github.com/ivanvanes18/hermes-bounded-stage.git
mkdir -p ~/.hermes/skills
cp -R hermes-bounded-stage ~/.hermes/skills/hermes-bounded-stage
```

Restart Hermes, then invoke the skill by name. The short `/bs` command is a local alias and is not installed automatically by this repository.

## Offline verification

No API key is required for the offline checks:

```bash
python3 -B ~/.hermes/skills/hermes-bounded-stage/scripts/stage_cli.py doctor
python3 -B -m unittest discover \
  -s ~/.hermes/skills/hermes-bounded-stage/scripts/tests -v
```

## Optional TypeSafe/Jev calls

Set `TYPESAFE_API_KEY` locally only when explicitly using a network-backed Jev action. Do not paste the key into chat or commit it to the repository. Offline validation and the test suite work without it.

## Documentation

- Core workflow: [`SKILL.md`](SKILL.md)
- Contract: [`references/contract.md`](references/contract.md)
- Executor routing: [`references/executor-routing.md`](references/executor-routing.md)
- Verification boundaries: [`references/verification.md`](references/verification.md)
- Standalone examples: [`examples/README.md`](examples/README.md)

## Status

The installed skill version is **1.2.0**. Real executor routes remain disabled until separately verified and authorized; installation alone does not enable external execution.
