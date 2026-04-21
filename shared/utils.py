"""shared/utils.py — Pure helpers used across multiple blueprints."""

import json
import re
import time

from shared.state import HISTORY_FILE, FAVORITES_FILE, MAX_HISTORY

# Common CLI commands that phone keyboards auto-capitalize.
_LOWERCASE_COMMANDS = {
    "apt",
    "awk",
    "bash",
    "bat",
    "black",
    "cargo",
    "cat",
    "cd",
    "chmod",
    "chown",
    "claude",
    "clear",
    "cp",
    "curl",
    "df",
    "diff",
    "dig",
    "docker",
    "du",
    "echo",
    "env",
    "exit",
    "export",
    "fd",
    "find",
    "free",
    "gcc",
    "git",
    "go",
    "grep",
    "head",
    "hostname",
    "htop",
    "ip",
    "isort",
    "java",
    "journalctl",
    "jq",
    "kill",
    "less",
    "ln",
    "ls",
    "make",
    "man",
    "mkdir",
    "mount",
    "mv",
    "mypy",
    "nano",
    "node",
    "nohup",
    "npm",
    "npx",
    "ping",
    "pip",
    "pkill",
    "ps",
    "pylint",
    "pytest",
    "python",
    "python3",
    "rg",
    "rm",
    "rmdir",
    "rsync",
    "rustc",
    "screen",
    "scp",
    "sed",
    "sh",
    "sort",
    "source",
    "ssh",
    "sudo",
    "systemctl",
    "tail",
    "tar",
    "tee",
    "tmux",
    "top",
    "touch",
    "tree",
    "uname",
    "unzip",
    "vim",
    "watch",
    "wc",
    "wget",
    "which",
    "whoami",
    "xargs",
    "yarn",
    "yq",
    "zip",
    "zsh",
}


def fix_first_word_case(text):
    """Lowercase the first word if it's a known CLI command.

    Phone keyboards auto-capitalize the first word (e.g., "Claude", "Git",
    "Docker"). This causes command-not-found errors in the terminal.
    """
    if not text:
        return text
    parts = text.split(None, 1)
    first = parts[0]
    if first.lower() in _LOWERCASE_COMMANDS and first != first.lower():
        return first.lower() + ((" " + parts[1]) if len(parts) > 1 else "")
    return text


def load_json(path, default=None):
    """Load a JSON file, returning *default* (empty dict) on any error."""
    if default is None:
        default = {}
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    """Write data as formatted JSON."""
    path.write_text(json.dumps(data, indent=2))


def clean_for_history(text):
    """Strip markdown/formatting noise so history entries are clean one-liners."""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        line = re.sub(r"<!--.*?-->", "", line)
        stripped = line.strip()
        if stripped.startswith("```"):
            continue
        if re.match(r"^[-=*]{3,}\s*$", stripped):
            continue
        stripped = re.sub(r"^#{1,6}\s+", "", stripped)
        cleaned.append(stripped)
    result = " ".join(cleaned)
    result = re.sub(r" {2,}", " ", result).strip()
    if len(result) > 500:
        result = result[:497] + "..."
    return result


def add_to_history(text):
    """Add a text entry to history.json (deduplicates, skips trivial entries)."""
    display = clean_for_history(text)
    if not display:
        return
    if len(text.strip()) <= 2:
        return
    history = load_json(HISTORY_FILE, default=[])
    history = [h for h in history if h["text"] != text]
    history.insert(
        0,
        {
            "text": text,
            "display": display,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )
    save_json(HISTORY_FILE, history[:MAX_HISTORY])


def resolve_target(data):
    """Return the tmux target to use: prefer client-supplied, fall back to global."""
    from shared.state import tmux_target

    client_target = (data.get("target") or "").strip()
    return client_target or tmux_target
