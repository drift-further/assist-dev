"""Process-oriented commands for the Claude Assist CLI."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from cli import http
from cli.config import Config


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


def start(resolved: Config) -> int:
    return _control(resolved, "start")


def stop(resolved: Config) -> int:
    return _control(resolved, "stop")


def restart(resolved: Config) -> int:
    return _control(resolved, "restart")


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
    return _control(resolved, "status")


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
    _check_command("xclip", "xclip")
    _check_command("xdotool", "xdotool")
    _check_command("docker", "docker")
    _check_command("curl", "curl")
    _check_command("nginx", "nginx")

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

    docker = shutil.which("docker")
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

    print()
    if ok_all:
        print("OK")
        return 0
    print("Problems found — see above")
    return 1


def studio(arguments: list[str]) -> int:
    if shutil.which("studio"):
        executable = "studio"
        argv = ["studio", *arguments]
    else:
        print_error("no studio CLI on PATH")
        return 1

    try:
        os.execvp(executable, argv)
    except OSError as exc:
        print_error(f"Unable to run {executable}: {exc}")
        return 1
