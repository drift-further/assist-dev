"""`assist service install|uninstall|status` — keep Assist running across reboots.

Linux: a systemd --user unit. macOS: a launchd agent. Both run
`assist-ctl run`, the foreground mode that sources .env and appends to the same
rotated log `assist logs` reads, so the service and a manual start behave the
same. Once a unit is installed for this checkout, `assist start/stop/restart/
status` go through it (proc.py), or the two would fight over the port.

Opt-in only: install.sh offers it, never installs it on its own.
"""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "drift-assist.service"
LAUNCHD_LABEL = "dev.driftassist.assist"


def _platform() -> str:
    return "darwin" if sys.platform == "darwin" else "linux"


def unit_path() -> Path:
    if _platform() == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "systemd" / "user" / UNIT_NAME


def systemd_unit(home: Path, path_env: str) -> str:
    # PATH is captured at install time: a user unit starts with systemd's
    # minimal PATH, and panes Assist opens would then find no claude or codex.
    return (
        "[Unit]\n"
        "Description=Drift Assist (phone-first web terminal)\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={home}\n"
        f"Environment=PATH={path_env}\n"
        f"ExecStart={home / 'assist-ctl'} run\n"
        # Signal only the Flask process on stop, restart and failure cleanup.
        # A tmux server Assist started is a child of this unit and sits in its
        # cgroup; the default KillMode=control-group would kill it -- and every
        # agent session in it -- whenever the web UI restarts.
        "KillMode=process\n"
        "Restart=on-failure\n"
        "RestartSec=3\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def launchd_plist(home: Path, path_env: str) -> bytes:
    return plistlib.dumps(
        {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [str(home / "assist-ctl"), "run"],
            "WorkingDirectory": str(home),
            "EnvironmentVariables": {"PATH": path_env},
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            # launchd kills the job's process group when the job exits. tmux
            # daemonizes into its own session, but say so explicitly: a tmux
            # server Assist started must outlive a restart of the web UI.
            "AbandonProcessGroup": True,
        }
    )


def installed_for(home: Path) -> bool:
    """True only if a unit exists AND it runs this checkout's assist-ctl."""
    try:
        text = unit_path().read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return str(Path(home) / "assist-ctl") in text


def _run(*command: str, check: bool = False) -> int:
    sys.stdout.flush()
    try:
        return subprocess.run(list(command), check=check).returncode
    except OSError as exc:
        print(f"assist: cannot run {command[0]}: {exc}", file=sys.stderr)
        return 1


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def control(verb: str) -> int:
    """start | stop | restart | status through the installed unit."""
    if _platform() == "darwin":
        target = f"{_gui_domain()}/{LAUNCHD_LABEL}"
        if verb == "start":
            return _run("launchctl", "bootstrap", _gui_domain(), str(unit_path()))
        if verb == "stop":
            return _run("launchctl", "bootout", target)
        if verb == "restart":
            return _run("launchctl", "kickstart", "-k", target)
        return _run("launchctl", "print", target)
    if verb == "status":
        return _run("systemctl", "--user", "status", "--no-pager", "--lines=0", UNIT_NAME)
    return _run("systemctl", "--user", verb, UNIT_NAME)


def _linger_enabled() -> bool:
    user = os.environ.get("USER") or ""
    try:
        out = subprocess.run(
            ["loginctl", "show-user", user, "-p", "Linger"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip() == "Linger=yes"


def install(resolved, stop_manual) -> int:
    """Write the unit, stop a manually started server, enable and start."""
    path = unit_path()
    other = path.exists() and not installed_for(resolved.home)
    if other:
        print(
            f"assist: {path} runs a different checkout; uninstall it from there first",
            file=sys.stderr,
        )
        return 1
    stop_manual()  # a nohup'd server would hold the port the unit needs
    path.parent.mkdir(parents=True, exist_ok=True)
    path_env = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    if _platform() == "darwin":
        path.write_bytes(launchd_plist(resolved.home, path_env))
        code = _run("launchctl", "bootstrap", _gui_domain(), str(path))
        print(f"Installed launchd agent {path}")
        return code
    path.write_text(systemd_unit(resolved.home, path_env), encoding="utf-8")
    _run("systemctl", "--user", "daemon-reload")
    code = _run("systemctl", "--user", "enable", "--now", UNIT_NAME)
    print(f"Installed systemd user unit {path}")
    if not _linger_enabled():
        print(
            "To keep it running while you are logged out (and start it at boot), run once:\n"
            f"  sudo loginctl enable-linger {os.environ.get('USER') or '$USER'}"
        )
    return code


def uninstall(resolved) -> int:
    path = unit_path()
    if not path.exists():
        print("No Assist service is installed.")
        return 0
    if not installed_for(resolved.home):
        print(f"assist: {path} belongs to a different checkout; leaving it", file=sys.stderr)
        return 1
    if _platform() == "darwin":
        _run("launchctl", "bootout", f"{_gui_domain()}/{LAUNCHD_LABEL}")
        path.unlink()
    else:
        _run("systemctl", "--user", "disable", "--now", UNIT_NAME)
        path.unlink()
        _run("systemctl", "--user", "daemon-reload")
    print(f"Removed {path}. Start manually with: assist start")
    return 0


def status(resolved) -> int:
    path = unit_path()
    if not path.exists():
        print(f"Not installed ({path}). Install with: assist service install")
        return 0
    if not installed_for(resolved.home):
        print(f"{path} runs a different checkout, not {resolved.home}")
        return 0
    print(f"Installed: {path}")
    return control("status")


def dispatch(resolved, action: str, stop_manual) -> int:
    if action == "install":
        return install(resolved, stop_manual)
    if action == "uninstall":
        return uninstall(resolved)
    return status(resolved)
