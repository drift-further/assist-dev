"""Process-oriented commands for the Drift Assist CLI."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from cli import http
from cli.config import Config
from shared.execution_park import FEATURES_PARKED


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"
MARK_OK = f"{GREEN}✓{RESET}"
MARK_FAIL = f"{RED}✗{RESET}"
MARK_WARN = f"{YELLOW}○{RESET}"


def print_error(message: str) -> None:
    print(f"{RED}ERROR:{RESET} {message}", file=sys.stderr)


def _control(resolved: Config, verb: str, arguments: list[str] | None = None, env=None) -> int:
    sys.stdout.flush()  # keep our own lines ahead of the child's when piped
    try:
        completed = subprocess.run(
            [str(resolved.home / "assist-ctl"), verb, *(arguments or [])],
            cwd=resolved.home,
            env=env,
        )
    except OSError as exc:
        print_error(f"Unable to run {resolved.home / 'assist-ctl'}: {exc}")
        return 1
    return completed.returncode


def _service_or_control(resolved: Config, verb: str) -> int:
    """Go through the installed service unit when there is one for this checkout.

    A manual assist-ctl start beside a running unit would fight it for the port.
    """
    from cli import service

    if service.installed_for(resolved.home):
        return service.control(verb)
    return _control(resolved, verb)


def _print_reach(resolved: Config) -> None:
    from cli.pair import phone_url

    print(f"  On this host: http://localhost:{resolved.port}/")
    url = phone_url(resolved.port)
    if url:
        print(f"  Phone:        {url}  (sign it in with: assist pair)")
    else:
        print("  Phone:        run `assist expose`, then `assist pair`")


def start(resolved: Config) -> int:
    code = _service_or_control(resolved, "start")
    if code == 0:
        _print_reach(resolved)
    return code


def stop(resolved: Config) -> int:
    return _service_or_control(resolved, "stop")


def stop_manual(resolved: Config) -> int:
    """Stop a server started with assist-ctl, bypassing any service unit."""
    return _control(resolved, "stop")


def restart(resolved: Config) -> int:
    return _service_or_control(resolved, "restart")


def activate_park(resolved: Config, invoked_home: Path, resume: bool = False) -> int:
    """Enter the sealed new-first activation controller.

    The home equality check precedes controller execution, hence precedes its
    lock, candidate child, signals, and listener.  A default-XDG marker may
    select a different checkout, but can never redirect this isolated CLI.
    """
    try:
        selected_home = resolved.home.resolve(strict=True)
        physical_invoked_home = invoked_home.resolve(strict=True)
    except OSError as exc:
        print_error(f"activation_home_unresolvable: {exc}")
        return 1
    if selected_home != physical_invoked_home:
        print_error(
            "activation_home_mismatch: "
            f"invoked={physical_invoked_home} resolved={selected_home}"
        )
        return 1

    assist_ctl = physical_invoked_home / "assist-ctl"
    serve_script = physical_invoked_home / "serve.py"
    if not assist_ctl.is_file() or not serve_script.is_file():
        print_error("activation_entrypoint_missing")
        return 1
    activation_env = os.environ.copy()
    activation_env.update(
        {
            "ASSIST_HOME": str(physical_invoked_home),
            "ASSIST_ACTIVATION_HOME": str(physical_invoked_home),
            "ASSIST_ACTIVATION_CTL": str(assist_ctl.resolve(strict=True)),
            "ASSIST_ACTIVATION_SERVE": str(serve_script.resolve(strict=True)),
            "ASSIST_PID_FILE": str(resolved.pid_file.resolve()),
            "ASSIST_LOG_FILE": str(resolved.log_file.resolve()),
            "ASSIST_CONTROL_DIR": str(resolved.control_dir.resolve()),
            "ASSIST_AUTH_TOKEN_PATH": str(resolved.auth_token_path.resolve()),
            "ASSIST_PORT": str(resolved.port),
        }
    )
    return _control(
        resolved,
        "activate-park-v16",
        ["--resume"] if resume else [],
        activation_env,
    )


def status(resolved: Config) -> int:
    return _service_or_control(resolved, "status")


def logs(resolved: Config, lines: str = "100", follow: bool = False) -> int:
    log_file = resolved.log_file
    if not log_file.is_file():
        print_error(f"Log file not found: {log_file}")
        return 1

    command = ["tail", "-f" if follow else "-n"]
    if not follow:
        command.append(str(lines))
    command.append(str(log_file))

    try:
        completed = subprocess.run(command)
    except OSError as exc:
        print_error(f"Unable to run tail: {exc}")
        return 1
    return completed.returncode


def config(resolved: Config) -> int:
    user_home = os.environ.get("HOME", "")
    projects_dir = os.environ.get("ASSIST_PROJECTS_DIR") or f"{user_home}/projects"
    skills_dir = os.environ.get("ASSIST_SKILLS_DIR") or f"{user_home}/.claude/skills"
    session_init_cmd = os.environ.get("ASSIST_SESSION_INIT_CMD") or "(none)"

    print(f"ASSIST_HOME        {resolved.home}")
    print(f"Port               {resolved.port}")
    print(f"Python             {resolved.python}")
    print(f"Venv               {resolved.home / '.venv'}")
    print(f"User config file   {resolved.config_file}")
    print(f"Env file           {resolved.home / '.env'}")
    print(f"Projects dir       {projects_dir}")
    print(f"Skills dir         {skills_dir}")
    print(f"Session init cmd   {session_init_cmd}")
    print(f"API base           {resolved.api}")
    return 0


def _tmux_version() -> tuple[int, int] | None:
    """(major, minor) from `tmux -V` ("tmux 3.7c", "tmux next-3.4"), or None."""
    try:
        output = subprocess.run(
            ["tmux", "-V"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)", output)
    return (int(match[1]), int(match[2])) if match else None


def _check_command(name: str, command: str, required: bool = False) -> bool:
    path = shutil.which(command)
    if path:
        print(f"  {MARK_OK} {name} ({path})")
        return True
    if required:
        print(f"  {MARK_FAIL} {name} (required)")
        return False
    print(f"  {MARK_WARN} {name} (optional)")
    return True


def doctor(resolved: Config) -> int:
    ok_all = True

    print("Environment:")
    ok_all = _check_command("python3", "python3", required=True) and ok_all
    ok_all = _check_command("tmux", "tmux", required=True) and ok_all
    tmux_version = _tmux_version()
    if tmux_version is not None and tmux_version < (3, 2):
        print(
            f"  {MARK_FAIL} tmux {tmux_version[0]}.{tmux_version[1]} is too old "
            "(3.2+ required: the control client needs `-f no-output`)"
        )
        ok_all = False
    _check_command("xclip", "xclip")
    _check_command("xdotool", "xdotool")
    if not FEATURES_PARKED:
        _check_command("docker", "docker")
    _check_command("curl", "curl")
    _check_command("nginx", "nginx (only for the proxy setup)")

    npx = shutil.which("npx")
    claude = shutil.which("claude")
    if npx:
        print(f"  {MARK_OK} npx ({npx})")
    elif claude:
        print(f"  {MARK_OK} claude ({claude})")
    else:
        print(
            f"  {MARK_WARN} npx/claude "
            "(neither found — Claude Code launch will fail)"
        )

    docker = None if FEATURES_PARKED else shutil.which("docker")
    if docker:
        try:
            docker_result = subprocess.run(
                [docker, "ps"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            docker_result = None
        if docker_result is not None and docker_result.returncode == 0:
            print(f"  {MARK_OK} docker daemon reachable")
        else:
            print(f"  {MARK_WARN} docker installed but user lacks permission")

    print()
    print("Install:")
    if os.access(resolved.python, os.X_OK):
        print(f"  {MARK_OK} venv ({resolved.python})")
    else:
        print(f"  {MARK_FAIL} venv missing — run install.sh")
        ok_all = False
    if (resolved.home / ".env").is_file():
        print(f"  {MARK_OK} .env present")
    else:
        print(f"  {MARK_WARN} .env missing — run install.sh")
    if resolved.config_file.is_file():
        print(f"  {MARK_OK} user config ({resolved.config_file})")
    else:
        print(f"  {MARK_WARN} user config missing")
    if os.access(resolved.home, os.W_OK):
        print(f"  {MARK_OK} data directory writable")
    else:
        print(f"  {MARK_FAIL} data directory NOT writable ({resolved.home})")
        ok_all = False

    print()
    print("Server:")
    try:
        http.get("/health")
    except http.ApiError:
        print(f"  {MARK_WARN} not running on :{resolved.port}")
    else:
        print(f"  {MARK_OK} responding on :{resolved.port}")
    from cli import service
    from cli.pair import phone_url

    if service.installed_for(resolved.home):
        print(f"  {MARK_OK} service unit installed ({service.unit_path()})")
    else:
        print(f"  {MARK_WARN} no service unit (optional: assist service install)")
    url = phone_url(resolved.port)
    if url:
        print(f"  {MARK_OK} phone URL: {url}")
    else:
        print(f"  {MARK_WARN} loopback only — reach it from a phone with: assist expose")

    print()
    if ok_all:
        print("OK")
        return 0
    print("Problems found — see above")
    return 1


def studio(arguments: list[str]) -> int:
    # `sto` is the name the Studio skill and hosted install teach; `studio`
    # is the older name, still accepted.
    executable = next((name for name in ("sto", "studio") if shutil.which(name)), None)
    if executable is None:
        print_error("no Studio CLI on PATH (looked for sto, then studio)")
        return 1
    argv = [executable, *arguments]

    try:
        os.execvp(executable, argv)
    except OSError as exc:
        print_error(f"Unable to run {executable}: {exc}")
        return 1
