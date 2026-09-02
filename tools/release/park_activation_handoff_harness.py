#!/usr/bin/env python3
"""Actual-entrypoint hermetic harness for activate-park-v16."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def path_identity(path: Path, *, appendable: bool = False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "type": "missing"}
    value = {
        "path": str(path),
        "device": info.st_dev,
        "inode": info.st_ino,
        "uid": info.st_uid,
        "mode": stat.S_IMODE(info.st_mode),
    }
    if stat.S_ISLNK(info.st_mode):
        value.update(type="symlink", target=os.readlink(path))
    elif stat.S_ISREG(info.st_mode):
        value["type"] = "file"
        if not appendable:
            value["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif stat.S_ISDIR(info.st_mode):
        value["type"] = "directory"
    else:
        value["type"] = "other"
    return value


def tracked_inventory(repo: Path):
    completed = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    result = {}
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="surrogateescape")
        result[relative] = path_identity(repo / relative)
    return result


def selected_entrypoints(root: Path):
    return {
        name: path_identity(root / name)
        for name in (
            "assist-ctl",
            "bin/assist",
            "cli/proc.py",
            "serve.py",
            "shared/park_activation.py",
        )
    }


def process_start(pid: int):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except OSError:
        return None
    if len(fields) <= 21 or fields[2] == "Z":
        return None
    return fields[21]


def listener_identity(port: int):
    inodes = set()
    wanted = f"{port:04X}"
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            rows = table.read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) > 9 and fields[1].rsplit(":", 1)[-1] == wanted and fields[3] == "0A":
                inodes.add(fields[9])
    owners = []
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            links = list((proc_dir / "fd").iterdir())
        except OSError:
            continue
        for link in links:
            try:
                target = os.readlink(link)
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                pid = int(proc_dir.name)
                owners.append({"pid": pid, "start_time": process_start(pid)})
                break
    return {"port": port, "socket_inodes": sorted(inodes), "owners": sorted(owners, key=lambda x: x["pid"])}


def snapshot_external(canonical: Path, assist_root: Path):
    return {
        "canonical_tracked": tracked_inventory(canonical),
        "dispatch_entrypoints": selected_entrypoints(assist_root),
        "default_xdg_marker": path_identity(
            Path.home() / ".config" / "claude-assist" / "config.env"
        ),
        "live_pid": path_identity(Path("/tmp/assist-server.pid")),
        "live_auth": path_identity(canonical / "auth_token"),
        "live_log": path_identity(Path("/tmp/assist-server.log"), appendable=True),
        "live_listener": listener_identity(8089),
    }


def inventory_tree(root: Path):
    result = {}
    for path in sorted(root.rglob("*"), key=lambda item: os.fsencode(str(item.relative_to(root)))):
        result[str(path.relative_to(root))] = path_identity(path)
    return result


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def stop_exact(pid: int, start_time: str | None):
    if not start_time or process_start(pid) != start_time:
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while process_start(pid) == start_time and time.monotonic() < deadline:
        time.sleep(0.02)
    if process_start(pid) == start_time:
        os.kill(pid, signal.SIGKILL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assist-root", type=Path, required=True)
    parser.add_argument("--canonical", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--positive-and-default-xdg-negative", action="store_true")
    mode.add_argument("--positive-only", action="store_true")
    args = parser.parse_args()

    root = args.run_root.resolve()
    assist_root = args.assist_root.resolve(strict=True)
    canonical = args.canonical.resolve(strict=True)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    home = root / "home"
    positive_xdg = root / "positive-xdg"
    default_config = home / ".config" / "claude-assist" / "config.env"
    positive_config = positive_xdg / "claude-assist" / "config.env"
    directories = [
        home,
        positive_config.parent,
        root / "control",
        root / "tmux",
    ]
    if args.positive_and_default_xdg_negative:
        directories.append(default_config.parent)
    for directory in directories:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)

    port = free_port()
    pid_file = root / "server.pid"
    log_file = root / "server.log"
    auth_token = root / "auth-token"
    control = root / "control"
    observer = root / "observer.json"
    provenance_root = root / "provenance"
    provenance_receipt = root / "provenance-init.json"
    observer.write_text(
        json.dumps(
            {
                "tmux": {"reachable": True, "rows": []},
                "docker": {"reachable": True, "rows": []},
                "metadata": {"barrier": "post-send-pre-docker", "adapter": "hermetic"},
            }
        ),
        encoding="utf-8",
    )
    os.chmod(observer, 0o600)
    positive_config.write_text(
        "\n".join(
            (
                f"ASSIST_HOME={assist_root}",
                f"ASSIST_PORT={port}",
                f"ASSIST_PID_FILE={pid_file}",
                f"ASSIST_LOG_FILE={log_file}",
                f"ASSIST_CONTROL_DIR={control}",
                f"ASSIST_AUTH_TOKEN_PATH={auth_token}",
                f"ASSIST_LAUNCH_PROVENANCE_ROOT={provenance_root}",
                "",
            )
        ),
        encoding="utf-8",
    )
    os.chmod(positive_config, 0o600)

    auth_token.write_text("hermetic-activation-token\n", encoding="utf-8")
    os.chmod(auth_token, 0o600)
    server_env = os.environ.copy()
    server_env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(positive_xdg),
            "ASSIST_HOME": str(assist_root),
            "ASSIST_PORT": str(port),
            "ASSIST_PID_FILE": str(pid_file),
            "ASSIST_LOG_FILE": str(root / "server.log"),
            "ASSIST_CONTROL_DIR": str(control),
            "ASSIST_AUTH_TOKEN_PATH": str(auth_token),
            "ASSIST_LAUNCH_PROVENANCE_ROOT": str(provenance_root),
            "TMUX": "",
            "TMUX_TMPDIR": str(root / "tmux"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    initialized = subprocess.run(
        [
            sys.executable,
            "-m",
            "shared.launch_provenance",
            "initialize",
            "--assist-home",
            str(assist_root),
            "--expect-empty",
            "--receipt",
            str(provenance_receipt),
        ],
        cwd=assist_root,
        env=server_env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if initialized.returncode != 0:
        raise AssertionError(f"provenance initialization failed: {initialized.stderr.strip()}")
    ambient = subprocess.run(
        ["tmux", "new-session", "-d", "-s", "ambient", "/bin/sh", "-c", "exec sleep 120"],
        env=server_env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if ambient.returncode != 0:
        raise AssertionError(f"private ambient tmux failed: {ambient.stderr.strip()}")
    pane_rows = subprocess.run(
        [
            "tmux", "list-panes", "-a", "-F",
            "#{pid}\t#{session_id}\t#{window_id}\t#{pane_id}\t#{pane_pid}\t#{session_name}:#{window_index}.#{pane_index}",
        ],
        env=server_env,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.splitlines()
    observer.write_text(
        json.dumps(
            {
                "tmux": {"reachable": True, "rows": pane_rows},
                "docker": {"reachable": True, "rows": []},
                "metadata": {"barrier": "post-send-pre-docker", "adapter": "hermetic"},
            }
        ),
        encoding="utf-8",
    )
    os.chmod(observer, 0o600)

    before = snapshot_external(canonical, assist_root)
    old = subprocess.Popen(
        [
            sys.executable,
            str(assist_root / "serve.py"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=assist_root,
        env=server_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    old_start = process_start(old.pid)
    pid_file.write_text(f"{old.pid}\n", encoding="ascii")
    os.chmod(pid_file, 0o600)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/settings",
                headers={"X-Assist-Token": "hermetic-activation-token"},
            )
            with urllib.request.urlopen(request, timeout=1) as response:
                settings = json.loads(response.read())
            if settings.get("pid") == old.pid:
                break
        except Exception:
            time.sleep(0.05)
    else:
        raise AssertionError("staged Assist listener did not become ready")
    candidate_pid = None
    candidate_start = None
    try:
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(positive_xdg),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMUX_TMPDIR": str(root / "tmux"),
            "ASSIST_PARK_OBSERVER_FIXTURE": str(observer),
        }
        completed = subprocess.run(
            [str(assist_root / "bin" / "assist"), "activate-park-v16"],
            cwd=assist_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise AssertionError(f"positive activation failed: {completed.stderr.strip()}")
        receipt_path = control / "activation-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        names = [event["name"] for event in receipt["events"]]
        required_order = [
            "candidate_ready",
            "old_frozen",
            "snapshot_ack",
            "old_dead",
            "candidate_bound",
        ]
        milestones = [name for name in names if name in required_order]
        if milestones != required_order:
            raise AssertionError(f"activation order mismatch: {names}")
        if names.index("port_owner_corroborated") > names.index("live_owned_predicate"):
            raise AssertionError("owned predicate preceded port corroboration")
        if names.index("live_owned_predicate") > names.index("candidate_ready"):
            raise AssertionError("candidate preceded owned predicate")
        if receipt.get("state") != "complete":
            raise AssertionError("activation receipt is not complete")
        sealed = receipt["sealed"]
        expected_paths = {
            "assist_home": assist_root,
            "assist_ctl": assist_root / "assist-ctl",
            "serve_script": assist_root / "serve.py",
            "pid_file": pid_file,
            "log_file": log_file,
            "control_dir": control,
            "auth_token": auth_token,
        }
        for key, expected in expected_paths.items():
            if Path(str(sealed[key])).resolve() != expected.resolve():
                raise AssertionError(f"sealed {key} mismatch")
        if int(sealed["port"]) != port:
            raise AssertionError("sealed port mismatch")
        metadata = receipt["snapshot"]["watermark"]["adapter"]
        if metadata.get("barrier") != "post-send-pre-docker":
            raise AssertionError("deterministic pre-Docker barrier missing")
        candidate_pid = int(pid_file.read_text(encoding="ascii").strip())
        candidate_start = process_start(candidate_pid)
        if candidate_start is None:
            raise AssertionError("published candidate generation absent")
    finally:
        if old.poll() is None:
            stop_exact(old.pid, old_start)
        try:
            old.wait(timeout=3)
        except subprocess.TimeoutExpired:
            old.kill()
            old.wait(timeout=3)
        if candidate_pid:
            stop_exact(candidate_pid, candidate_start)
        # -S is explicit on purpose: this must tear down ONLY the sandbox server
        # under server_env["TMUX_TMPDIR"]. A bare argv would inherit whatever the
        # caller's environment points at and take down the real tmux server and
        # every live pane with it. See tests/test_launch_provenance.py.
        subprocess.run(
            [
                "tmux",
                "-S",
                str(Path(server_env["TMUX_TMPDIR"]) / f"tmux-{os.getuid()}" / "default"),
                "kill-server",
            ],
            env=server_env,
            capture_output=True,
            timeout=5,
        )

    if args.positive_and_default_xdg_negative:
        default_config.write_text(f"ASSIST_HOME={canonical}\n", encoding="utf-8")
        os.chmod(default_config, 0o600)
        private_before_negative = inventory_tree(root)
        negative_env = {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMUX_TMPDIR": str(root / "tmux"),
        }
        negative = subprocess.run(
            [str(assist_root / "bin" / "assist"), "activate-park-v16"],
            cwd=assist_root,
            env=negative_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if negative.returncode == 0 or "activation_home_mismatch" not in negative.stderr:
            raise AssertionError("default-XDG mismatch did not fail before dispatch")
        if inventory_tree(root) != private_before_negative:
            raise AssertionError("negative branch mutated private state")

    after = snapshot_external(canonical, assist_root)
    if before != after:
        raise AssertionError("canonical/live identity changed during hermetic handoff")
    print("positive: candidate_ready < old_frozen < snapshot_ack < old_dead < candidate_bound")
    if args.positive_and_default_xdg_negative:
        print("negative: activation_home_mismatch before controller mutation")
    print("external identities: unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
