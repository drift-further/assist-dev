"""Container commands for the Claude Assist CLI."""

import json
import sys
import time

from cli import http
from cli.proc import print_error


HELP_TEXT = """Usage: assist container <subcommand>

Subcommands:
  status                 Show image info + running claude-session-* containers
  build                  Trigger a container image build and stream the log
  config                 Print the current container build config
  extensions             List registered extensions (SDK/tool bundles)
  kill <name>            Kill a running claude-session-* container

"""
SUBCOMMANDS = "status | build | config | extensions | kill <name>"


def _require_server() -> None:
    try:
        http.get("/health")
    except http.ApiError as exc:
        if exc.kind == "auth":
            raise
        raise http.server_not_running(exc.api) from exc


def _pretty_print(data: dict) -> None:
    print(json.dumps(data, indent=4))


def status() -> int:
    _require_server()
    _pretty_print(http.get("/api/container/status"))
    return 0


def config() -> int:
    _require_server()
    _pretty_print(http.get("/api/container/config"))
    return 0


def extensions() -> int:
    _require_server()
    _pretty_print(http.get("/api/container/extensions"))
    return 0


def kill(name: str) -> int:
    if not name:
        print_error("Usage: assist container kill <container-name>")
        return 1
    _require_server()
    _pretty_print(http.post(f"/api/container/kill/{name}"))
    return 0


def build() -> int:
    _require_server()

    try:
        response = http.get("/api/container/build/status")
    except Exception as exc:
        print(f"Could not reach build status API: {exc}", file=sys.stderr)
        return 2

    if response.get("active"):
        print("==> Build already in progress — attaching to live log")
    else:
        print("==> Triggering container build")
        try:
            result = http.post("/api/container/build", data={})
        except Exception as exc:
            print(f"Failed to start build: {exc}", file=sys.stderr)
            return 2
        if not result.get("ok"):
            print(f"Failed to start build: {result.get('error')}", file=sys.stderr)
            return 2

    last = 0
    while True:
        try:
            response = http.get("/api/container/build/status")
        except Exception as exc:
            print(f"(poll error: {exc})", file=sys.stderr)
            time.sleep(2)
            continue
        log = response.get("log") or []
        for line in log[last:]:
            print(line, flush=True)
        last = len(log)
        if not response.get("active"):
            success = response.get("success")
            print()
            if success is True:
                print("\033[32m✓ build succeeded\033[0m")
                return 0
            if success is False:
                print("\033[31m✗ build failed\033[0m")
                return 1
            print("(build finished with unknown status)")
            return 0
        time.sleep(1)


def dispatch(arguments: list[str]) -> int:
    subcommand = arguments[0] if arguments else "status"
    remaining = arguments[1:]

    if subcommand == "status":
        return status()
    if subcommand == "build":
        return build()
    if subcommand == "config":
        return config()
    if subcommand == "extensions":
        return extensions()
    if subcommand == "kill":
        return kill(remaining[0] if remaining else "")
    if subcommand in {"help", "-h", "--help"}:
        print(HELP_TEXT, end="")
        return 0

    print_error(f"Unknown container subcommand: {subcommand} (try: {SUBCOMMANDS})")
    return 1
