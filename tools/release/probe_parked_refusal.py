#!/usr/bin/env python3
"""Read-only probe for the compiled parked Automate refusal.

The probe never starts/stops Assist and never prints token bytes.  Its default
operation is a status read; an explicit ``--exercise-refusal`` performs the
named refused request and verifies the exact public response body.
"""

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path


def _request(url, token, method="GET", payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("X-Assist-Token", token)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def main():
    parser = argparse.ArgumentParser(description="Read-only Assist park/refusal probe")
    parser.add_argument("--base-url", default="http://127.0.0.1:8089")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--exercise-refusal", action="store_true")
    args = parser.parse_args()
    if args.token_file is None:
        parser.error("--token-file is required unless only requesting --help")
    token = args.token_file.read_text(encoding="utf-8").strip()
    if args.exercise_refusal:
        status, body = _request(
            args.base_url + "/api/automate/start",
            token,
            method="POST",
            payload={},
        )
        expected = {
            "ok": False,
            "error": "container_launch_parked",
            "reason": (
                "Container launch automation is temporarily parked while host wiring "
                "migrates."
            ),
            "intent": "automate_start",
        }
        if status != 409 or body != expected:
            raise SystemExit("park refusal mismatch")
    else:
        status, body = _request(args.base_url + "/api/automate/status", token)
        if status != 200 or body.get("park_phase") not in {"draining", "parked"}:
            raise SystemExit("park status mismatch")
    print("park probe: verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
