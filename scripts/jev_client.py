"""Pinned TypeSafe Choice client. No SDK, redirects, proxies, or automatic retries.

The HTTP exchange runs in a killable subprocess so a socket stall cannot keep
this controller call alive indefinitely. Input/output bodies are never logged.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

from runtime_support import canonical, parse_json, StageError

MODEL = "jev-1.13.0"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MAX_BODY = 128 * 1024


class JevError(Exception):
    pass


def probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def validate_response(reply: dict, questions: dict) -> dict:
    if not isinstance(reply, dict) or reply.get("model") != MODEL:
        raise JevError("unexpected_model")
    answers = reply.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise JevError("answer_coverage")
    usage = reply.get("usage")
    if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0
                                          for k in ("input_tokens", "output_tokens")):
        raise JevError("invalid_usage")
    for key, answer in answers.items():
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            raise JevError("invalid_answer_type")
        options = questions[key]["criteria"]
        selected = answer.get("choice")
        values = answer.get("probabilities")
        if not isinstance(selected, str) or selected not in options:
            raise JevError("unknown_option")
        if not isinstance(values, dict) or set(values) != set(options):
            raise JevError("option_coverage")
        if not all(probability(v) for v in values.values()) or not probability(answer.get("confidence")):
            raise JevError("invalid_probability")
        if abs(sum(values.values()) - 1) > 1e-6 or values[selected] < max(values.values()) - 1e-9:
            raise JevError("invalid_distribution")
    return answers


class JevClient:
    def __init__(self, timeout_seconds: float = 15):
        self.timeout_seconds = max(0.1, min(float(timeout_seconds), 15.0))

    def evaluate(self, state: dict, questions: dict) -> dict:
        key = os.environ.get("TYPESAFE_API_KEY", "")
        if not key:
            raise JevError("missing_key")
        data = canonical({"model": MODEL, "state": state, "questions": questions})
        if len(data) > MAX_BODY:
            raise JevError("payload_too_large")
        # No inherited provider tokens, proxy settings, or verbose-debug flags.
        environment = {"TYPESAFE_API_KEY": key, "PYTHONIOENCODING": "utf-8"}
        try:
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--http"], input=data,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment,
                timeout=self.timeout_seconds, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise JevError("unavailable") from exc
        if completed.returncode != 0 or len(completed.stdout) > MAX_BODY:
            raise JevError("unavailable")
        try:
            reply = parse_json(completed.stdout)
        except StageError as exc:
            raise JevError("invalid_response") from exc
        validate_response(reply, questions)
        return reply


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise JevError("redirect_refused")


def _http_main() -> int:
    try:
        payload = sys.stdin.buffer.read(MAX_BODY + 1)
        if len(payload) > MAX_BODY:
            return 2
        body = parse_json(payload)
        if body.get("model") != MODEL:
            return 2
        key = os.environ.get("TYPESAFE_API_KEY")
        if not key:
            return 2
        request = urllib.request.Request(
            ENDPOINT, data=payload, method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                return 2
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            return 2
        parse_json(raw)
        sys.stdout.buffer.write(raw)
        return 0
    except Exception:
        # Deliberately do not reveal HTTP bodies, headers, or exception strings.
        return 2


if __name__ == "__main__":
    raise SystemExit(_http_main() if sys.argv[1:] == ["--http"] else 2)
