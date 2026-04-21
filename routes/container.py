"""routes/container.py — Container management: build, status, packages, extensions."""

import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request

from shared import state

container_bp = Blueprint("container_bp", __name__)

DOCKER_DIR = Path(__file__).resolve().parent.parent / "docker"

# ---------------------------------------------------------------------------
# Build state — shared across threads
# ---------------------------------------------------------------------------
_build = {"active": False, "log": [], "started_at": None, "success": None}
_build_lock = threading.Lock()


# ---------------------------------------------------------------------------
# GET /api/container/status — image info + running containers
# ---------------------------------------------------------------------------
@container_bp.route("/api/container/status")
def container_status():
    cfg = state.get_container_config()
    image_name = cfg["image"]["name"]

    # Image info via docker images
    image_info = None
    try:
        proc = subprocess.run(
            [
                "docker",
                "images",
                image_name,
                "--format",
                "{{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in proc.stdout.strip().split("\n"):
            if line.strip():
                parts = line.split("\t")
                if len(parts) >= 3:
                    image_info = {
                        "name": parts[0],
                        "size": parts[1],
                        "created": parts[2],
                    }
                    break
    except Exception:
        pass

    # Running containers matching claude-session-*
    containers = []
    try:
        proc = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                "name=claude-session-",
                "--format",
                "{{.Names}}\t{{.Status}}\t{{.RunningFor}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in proc.stdout.strip().split("\n"):
            if line.strip():
                parts = line.split("\t")
                if len(parts) >= 2:
                    containers.append(
                        {
                            "name": parts[0],
                            "status": parts[1],
                            "running_for": parts[2] if len(parts) >= 3 else "",
                        }
                    )
    except Exception:
        pass

    return jsonify(
        {
            "ok": True,
            "image": image_info,
            "built_at": cfg["image"].get("built_at"),
            "config": cfg,
            "containers": containers,
        }
    )


# ---------------------------------------------------------------------------
# GET /api/container/config — return current config
# ---------------------------------------------------------------------------
@container_bp.route("/api/container/config")
def container_config_get():
    return jsonify({"ok": True, "config": state.get_container_config()})


# ---------------------------------------------------------------------------
# PATCH /api/container/config — update config
# ---------------------------------------------------------------------------
@container_bp.route("/api/container/config", methods=["PATCH"])
def container_config_patch():
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"ok": False, "error": "No data"}), 400
    updated = state.patch_container_config(data)
    return jsonify({"ok": True, "config": updated})


# ---------------------------------------------------------------------------
# POST /api/container/build — trigger image build in background
# ---------------------------------------------------------------------------
@container_bp.route("/api/container/build", methods=["POST"])
def container_build():
    with _build_lock:
        if _build["active"]:
            return jsonify({"ok": False, "error": "Build already in progress"}), 409

    threading.Thread(target=_run_build, daemon=True).start()
    return jsonify({"ok": True, "message": "Build started"})


# ---------------------------------------------------------------------------
# GET /api/container/build/status — return build log + active flag
# ---------------------------------------------------------------------------
@container_bp.route("/api/container/build/status")
def container_build_status():
    with _build_lock:
        return jsonify(
            {
                "ok": True,
                "active": _build["active"],
                "log": list(_build["log"]),
                "success": _build["success"],
            }
        )


# ---------------------------------------------------------------------------
# GET /api/container/extensions — list all extensions
# ---------------------------------------------------------------------------
@container_bp.route("/api/container/extensions")
def extensions_list():
    return jsonify({"ok": True, "extensions": state.get_extensions()})


# ---------------------------------------------------------------------------
# POST /api/container/extensions — add custom extension
# ---------------------------------------------------------------------------
@container_bp.route("/api/container/extensions", methods=["POST"])
def extensions_add():
    data = request.get_json(silent=True) or {}
    if not data.get("name"):
        return jsonify({"ok": False, "error": "Name is required"}), 400

    ext_id = data.get("id") or data["name"].lower().replace(" ", "-")
    ext = {
        "id": ext_id,
        "name": data["name"],
        "builtin": False,
        "archive": data.get("archive") or None,
        "install": data.get("install", []),
        "env": data.get("env", {}),
        "path_add": data.get("path_add") or None,
        "enabled": data.get("enabled", True),
    }
    extensions = state.add_extension(ext)
    return jsonify({"ok": True, "extensions": extensions})


# ---------------------------------------------------------------------------
# PATCH /api/container/extensions/<id> — update/toggle extension
# ---------------------------------------------------------------------------
@container_bp.route("/api/container/extensions/<ext_id>", methods=["PATCH"])
def extensions_update(ext_id):
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"ok": False, "error": "No data"}), 400
    extensions = state.update_extension(ext_id, data)
    return jsonify({"ok": True, "extensions": extensions})


# ---------------------------------------------------------------------------
# DELETE /api/container/extensions/<id> — delete (block if builtin)
# ---------------------------------------------------------------------------
@container_bp.route("/api/container/extensions/<ext_id>", methods=["DELETE"])
def extensions_delete(ext_id):
    # Check if builtin
    for ext in state.get_extensions():
        if ext.get("id") == ext_id and ext.get("builtin"):
            return (
                jsonify({"ok": False, "error": "Cannot delete built-in extension"}),
                400,
            )
    extensions = state.delete_extension(ext_id)
    return jsonify({"ok": True, "extensions": extensions})


# ---------------------------------------------------------------------------
# POST /api/container/kill/<name> — kill a claude-session container
# ---------------------------------------------------------------------------
@container_bp.route("/api/container/kill/<name>", methods=["POST"])
def container_kill(name):
    if not name.startswith("claude-session-"):
        return jsonify({"ok": False, "error": "Invalid container name"}), 400
    try:
        proc = subprocess.run(
            ["docker", "kill", name],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0:
            return jsonify({"ok": True, "killed": name})
        return jsonify({"ok": False, "error": proc.stderr.strip()}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Build implementation
# ---------------------------------------------------------------------------
def _generate_extensions_script():
    """Generate extensions-install.sh from enabled extensions."""
    extensions = state.get_extensions()
    lines = ["#!/bin/bash", "set -e", ""]

    enabled = [e for e in extensions if e.get("enabled")]
    if not enabled:
        lines.append("echo 'No extensions enabled'")
    else:
        for ext in enabled:
            name = ext.get("name", "unknown")
            lines.append(f"echo '=== Installing extension: {name} ==='")
            for cmd in ext.get("install", []):
                lines.append(cmd)
            # Set up env vars
            for k, v in ext.get("env", {}).items():
                lines.append(f"echo 'export {k}={v}' >> /etc/profile.d/extensions.sh")
            # PATH additions
            path_add = ext.get("path_add")
            if path_add:
                lines.append(
                    f"echo 'export PATH={path_add}:$PATH' >> /etc/profile.d/extensions.sh"
                )
            lines.append("")

    script_path = DOCKER_DIR / "extensions-install.sh"
    script_path.write_text("\n".join(lines) + "\n")
    return script_path


def _run_build():
    """Background thread: run docker build with args from config."""
    with _build_lock:
        _build["active"] = True
        _build["log"] = []
        _build["started_at"] = time.time()
        _build["success"] = None

    try:
        cfg = state.get_container_config()
        image_name = cfg["image"]["name"]

        _build["log"].append("Generating extensions-install.sh...")
        _generate_extensions_script()

        # Build args from config
        build_args = {
            "NODE_VERSION": cfg["base"]["node_version"],
            "PYTHON_VERSION": cfg["base"]["python_version"],
            "CACHEBUST_CLAUDE": str(int(time.time())),
            "PIP_PACKAGES": " ".join(cfg["packages"].get("pip", [])),
            "SYSTEM_PACKAGES": " ".join(cfg["packages"].get("system", [])),
        }

        cmd = ["docker", "build", "-t", image_name]
        for k, v in build_args.items():
            cmd.extend(["--build-arg", f"{k}={v}"])
        cmd.append(str(DOCKER_DIR))

        _build["log"].append(f"Running: docker build -t {image_name} ...")
        _build["log"].append("")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        for line in proc.stdout:
            _build["log"].append(line.rstrip())

        proc.wait()

        if proc.returncode == 0:
            _build["log"].append("")
            _build["log"].append("Build completed successfully.")
            _build["success"] = True

            # Update built_at timestamp
            state.patch_container_config(
                {"image": {"built_at": datetime.now(timezone.utc).isoformat()}}
            )
        else:
            _build["log"].append("")
            _build["log"].append(f"Build failed with exit code {proc.returncode}")
            _build["success"] = False

    except Exception as e:
        _build["log"].append(f"Build error: {e}")
        _build["success"] = False

    finally:
        with _build_lock:
            _build["active"] = False
