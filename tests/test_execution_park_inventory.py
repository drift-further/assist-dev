"""Static first-party execution inventory for ``assist-execution-park-v1``.

The inventory is intentionally explicit.  Adding a subprocess, background
thread, or PTY-delivery helper to the shipped server creates a new discovered
function and fails this gate until its trusted provenance is classified.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from shared import execution_park as park


ROOT = Path(__file__).resolve().parents[1]
EFFECT_CALLEES = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "threading.Thread",
        "concurrent.futures.ThreadPoolExecutor",
        "tmux_send_text",
        "tmux_send_keys",
        "send_keys",
    }
)


FUNCTION_INVENTORY: dict[str, str] = {}


def _classify(result: str, *functions: str) -> None:
    for function in functions:
        if function in FUNCTION_INVENTORY:
            raise AssertionError(f"duplicate inventory entry: {function}")
        FUNCTION_INVENTORY[function] = result


_classify("fixed:observe", "serve.py:start_application_backgrounds")
_classify(
    "intent:automate_trust_answer|automate_auto_answer",
    "routes/automate.py:_automate_scheduled_answer.effect",
)
_classify(
    "fixed:observe",
    "routes/automate.py:automate_recover",
    "routes/automate.py:automate_reconnect",
    "routes/automate.py:_automate_monitor_iteration",
)
_classify("intent:automate_start", "routes/automate.py:_automate_start_inner")
_classify(
    "intent:automate_start|automate_hard_relaunch|stop",
    "routes/automate.py:_automate_cleanup",
)
_classify(
    "intent:automate_soft_clear", "routes/automate.py:_automate_soft_clear.effect"
)
_classify(
    "intent:automate_soft_resend",
    "routes/automate.py:_automate_soft_resend.effect",
)
_classify(
    "intent:automate_hard_relaunch",
    "routes/automate.py:_automate_relaunch_effect",
)

_classify(
    "fixed:observe",
    "routes/autoyes.py:_autoyes_scan_tick",
    "routes/autoyes.py:restore_autoyes_from_settings",
)

_classify("intent:saved_command", "routes/commands.py:_run_command_effect")
_classify("intent:stop", "routes/commands.py:_stop_command_effect")
_classify("fixed:observe", "routes/commands.py:check_split_pane")
_classify("fixed:observe", "routes/completion.py:_session_cwd")

_classify("fixed:observe", "routes/container.py:container_status")
_classify(
    "intent:configured_image_build",
    "routes/container.py:_container_build_effect",
    "routes/container.py:_run_build",
)
_classify("intent:stop", "routes/container.py:_container_kill_effect")
_classify("fixed:observe", "routes/drafts.py:start_sweeper")

_classify(
    "intent:fixed_git",
    "routes/git.py:_git_run_effect",
    "routes/git.py:_git_run_effect._run_git",
)
_classify("intent:project_venv", "routes/git.py:_venv_create_effect")

_classify(
    "fixed:observe",
    "routes/poll.py:_run_git",
    "routes/poll.py:get_claude_meta",
    "routes/poll.py:consolidated_poll",
)
_classify("intent:configured_cli_proxy", "routes/poll.py:_cli_proxy_effect")
_classify("intent:configured_restart", "routes/settings.py:_restart_server_effect")

_classify("fixed:observe", "routes/streaming.py:_ensure_streamer")
_classify("fixed:observe", "routes/studio.py:_pane_cwd")

_classify("fixed:observe", "routes/terminal.py:terminal_launch")
_classify(
    "intent:terminal_init_launch|bare_terminal",
    "routes/terminal.py:_terminal_launch_effect",
)
_classify(
    "fixed:observe",
    "routes/terminal.py:enrich_panes_with_agents",
    "routes/terminal.py:terminal_sessions",
    "routes/terminal.py:terminal_cwd",
    "routes/terminal.py:terminal_duplicate",
)
_classify(
    "fixed:no_delivery",
    "routes/terminal.py:terminal_resize",
    "routes/terminal.py:terminal_unpin",
    "routes/terminal.py:terminal_clear",
    "routes/terminal.py:terminal_rename",
)
_classify("intent:stop", "routes/terminal.py:_terminal_kill_effect")
_classify(
    "intent:terminal_init_duplicate|bare_terminal",
    "routes/terminal.py:_terminal_duplicate_effect",
)
_classify("intent:terminal_run_init", "routes/terminal.py:_terminal_run_init_effect")
_classify(
    "intent:native_folder_picker", "routes/terminal.py:_terminal_explore_pick_effect"
)

_classify(
    "intent:autoyes_answer|operator_interactive|client_session_resume|client_session_restart|bare_terminal|observe",
    "shared/tmux.py:__init__",
)
_classify(
    "fixed:observe",
    "shared/tmux.py:capture_pane",
    "shared/tmux.py:pane_awaits_secret",
    "shared/tmux.py:tmux_target_exists",
)
_classify("intent:operator_interactive", "shared/tmux.py:get_clipboard")
_classify(
    "intent:automate_start|automate_hard_relaunch|automate_soft_clear|automate_soft_resend|automate_trust_answer|automate_auto_answer|terminal_init_launch|terminal_init_duplicate|terminal_run_init|saved_command|project_venv|fixed_git",
    "shared/tmux.py:tmux_send_keys",
    "shared/tmux.py:tmux_send_text",
)
_classify(
    "intent:automate_start|automate_hard_relaunch|terminal_init_launch|terminal_init_duplicate|bare_terminal|saved_command|fixed_git",
    "shared/tmux.py:_create_tmux_resource",
)


PARK_BINDINGS = {
    "routes/automate.py:automate_start": ("AUTOMATE_START",),
    "routes/automate.py:_automate_relaunch": ("AUTOMATE_HARD_RELAUNCH",),
    "routes/automate.py:_automate_soft_clear": ("AUTOMATE_SOFT_CLEAR",),
    "routes/automate.py:_automate_soft_resend": ("AUTOMATE_SOFT_RESEND",),
    "routes/automate.py:_automate_scheduled_answer": (
        "AUTOMATE_TRUST_ANSWER",
        "AUTOMATE_AUTO_ANSWER",
    ),
    "routes/autoyes.py:_deliver_autoyes_answer": ("AUTOYES_ANSWER",),
    "routes/input.py:send_key": ("OPERATOR_INTERACTIVE",),
    "routes/input.py:type_text": ("OPERATOR_INTERACTIVE",),
    "routes/input.py:type_client_resume": ("CLIENT_SESSION_RESUME",),
    "routes/input.py:type_client_restart": ("CLIENT_SESSION_RESTART",),
    "routes/terminal.py:terminal_launch": (
        "TERMINAL_INIT_LAUNCH",
        "BARE_TERMINAL",
    ),
    "routes/terminal.py:terminal_duplicate": (
        "TERMINAL_INIT_DUPLICATE",
        "BARE_TERMINAL",
    ),
    "routes/terminal.py:terminal_run_init": ("TERMINAL_RUN_INIT",),
    "routes/terminal.py:terminal_explore_pick": ("NATIVE_FOLDER_PICKER",),
    "routes/settings.py:restart_server": ("CONFIGURED_RESTART",),
    "routes/commands.py:run_command": ("SAVED_COMMAND",),
    "routes/git.py:git_run": ("FIXED_GIT",),
    "routes/git.py:venv_create": ("PROJECT_VENV",),
    "routes/poll.py:cli_proxy": ("CONFIGURED_CLI_PROXY",),
    "routes/container.py:container_build": ("CONFIGURED_IMAGE_BUILD",),
}


DIRECT_RETURN_BINDINGS = {
    "routes/automate.py:automate_stop": "STOP",
    "routes/autoyes.py:_deliver_autoyes_answer": "AUTOYES_ANSWER",
    "routes/commands.py:stop_command": "STOP",
    "routes/container.py:container_config_patch": "IMAGE_CONFIG",
    "routes/container.py:extensions_add": "IMAGE_CONFIG",
    "routes/container.py:extensions_update": "IMAGE_CONFIG",
    "routes/container.py:extensions_delete": "IMAGE_CONFIG",
    "routes/container.py:container_kill": "STOP",
    "routes/input.py:send_key": "OPERATOR_INTERACTIVE",
    "routes/input.py:type_text": "OPERATOR_INTERACTIVE",
    "routes/input.py:type_client_resume": "CLIENT_SESSION_RESUME",
    "routes/input.py:type_client_restart": "CLIENT_SESSION_RESTART",
    "routes/terminal.py:terminal_launch": "BARE_TERMINAL",
    "routes/terminal.py:terminal_kill": "STOP",
}


DENIED_HTTP_BINDINGS = {
    "routes/automate.py:automate_start": "AUTOMATE_START",
    "routes/poll.py:cli_proxy": "CONFIGURED_CLI_PROXY",
    "routes/container.py:container_build": "CONFIGURED_IMAGE_BUILD",
}


CLIENT_ENDPOINTS = {
    "/api/automate/start": "intent:automate_start",
    "/api/automate/stop": "intent:stop",
    "/api/commands/run": "intent:saved_command",
    "/api/commands/stop": "intent:stop",
    "/api/git/run": "intent:fixed_git",
    "/api/venv/create": "intent:project_venv",
    "/api/restart": "intent:configured_restart",
    "/api/container/build": "intent:configured_image_build",
    "/api/container/config": "intent:image_config",
    "/api/container/extensions": "intent:image_config",
    "/api/container/kill/": "intent:stop",
    "/terminal/explore/pick": "intent:native_folder_picker",
    "/terminal/launch": "intent:terminal_init_launch|bare_terminal",
    "/terminal/duplicate": "intent:terminal_init_duplicate|bare_terminal",
    "/terminal/run-init": "intent:terminal_run_init",
    "/terminal/kill": "intent:stop",
    "/type": "intent:operator_interactive",
    "/type/client-resume": "intent:client_session_resume",
    "/type/client-restart": "intent:client_session_restart",
    "/key": "intent:operator_interactive",
    "/terminal/resize": "fixed:no_delivery",
    "/terminal/clear": "fixed:no_delivery",
    "/terminal/unpin": "fixed:no_delivery",
    "/terminal/rename": "fixed:no_delivery",
}


BACKGROUND_CALLBACKS = {
    "serve.py:autoyes_scanner": "fixed:observe|intent:autoyes_answer",
    "serve.py:studio_refresher": "fixed:observe",
    "routes/automate.py:_automate_monitor": "fixed:observe|intent:automate_hard_relaunch|automate_soft_clear|automate_soft_resend|automate_trust_answer|automate_auto_answer",
    "routes/container.py:_run_build": "intent:configured_image_build",
    "routes/drafts.py:drafts_sweeper": "fixed:observe",
    "routes/streaming.py:_terminal_streamer": "fixed:observe",
    "routes/git.py:_run_git": "intent:fixed_git",
}


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


class _EffectVisitor(ast.NodeVisitor):
    def __init__(self, relative: str):
        self.relative = relative
        self.stack: list[str] = []
        self.effects: dict[str, set[str]] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        callee = _dotted(node.func)
        if callee in EFFECT_CALLEES:
            function = ".".join(self.stack) if self.stack else "<module>"
            self.effects.setdefault(f"{self.relative}:{function}", set()).add(callee)
        self.generic_visit(node)


def _function_source(relative: str, function_name: str) -> str:
    source = (ROOT / relative).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function not found: {relative}:{function_name}")


class ExecutionParkInventoryTests(unittest.TestCase):
    def test_every_process_thread_and_pty_site_is_classified(self):
        discovered: dict[str, set[str]] = {}
        sources = [
            ROOT / "serve.py",
            *sorted((ROOT / "routes").glob("*.py")),
            ROOT / "shared" / "tmux.py",
        ]
        for path in sources:
            relative = path.relative_to(ROOT).as_posix()
            visitor = _EffectVisitor(relative)
            visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
            discovered.update(visitor.effects)

        self.assertEqual(
            set(discovered),
            set(FUNCTION_INVENTORY),
            "every new process/thread/PTY function needs an inventory result",
        )
        intent_values = {intent.value for intent in park.Intent}
        for function, result in FUNCTION_INVENTORY.items():
            with self.subTest(function=function, result=result):
                if result.startswith("fixed:"):
                    self.assertIn(result, {"fixed:observe", "fixed:no_delivery"})
                    continue
                self.assertTrue(result.startswith("intent:"))
                for intent in result.removeprefix("intent:").split("|"):
                    self.assertIn(intent, intent_values)

    def test_park_bindings_name_only_compiled_provenance(self):
        background_funnel = _function_source(
            "routes/automate.py", "_perform_automate_background"
        )
        self.assertIn("park.perform", background_funnel)
        for qualified, intent_names in PARK_BINDINGS.items():
            relative, function = qualified.split(":", 1)
            source = _function_source(relative, function)
            with self.subTest(binding=qualified):
                if relative == "routes/automate.py" and function != "automate_start":
                    self.assertIn("_perform_automate_background", source)
                else:
                    self.assertIn("park.perform", source)
                for intent_name in intent_names:
                    self.assertIn(f"park.Intent.{intent_name}", source)
        perform_source = _function_source("shared/execution_park.py", "perform")
        for lexical in ("command", "path", "pane", "target", "foreground", "resolve"):
            self.assertNotIn(lexical, perform_source)

    def test_direct_flask_returns_can_only_use_allowed_intents(self):
        for qualified, intent_name in DIRECT_RETURN_BINDINGS.items():
            relative, function = qualified.split(":", 1)
            source = _function_source(relative, function)
            intent = park.Intent[intent_name]
            with self.subTest(binding=qualified, intent=intent.value):
                self.assertIn("return park.perform", source)
                self.assertIn(f"park.Intent.{intent_name}", source)
                self.assertIn(intent, park.ALLOWED_INTENTS)

    def test_denied_http_bindings_convert_refusals_to_responses(self):
        for qualified, intent_name in DENIED_HTTP_BINDINGS.items():
            relative, function = qualified.split(":", 1)
            source = _function_source(relative, function)
            intent = park.Intent[intent_name]
            with self.subTest(binding=qualified, intent=intent.value):
                self.assertIn(f"park.Intent.{intent_name}", source)
                self.assertIn(intent, park.DENIED_INTENTS)
                self.assertIn("park.is_refusal", source)

    def test_javascript_and_cli_execution_endpoints_are_inventoried(self):
        js_text = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted((ROOT / "js").glob("*.js"))
        )
        cli_session = (ROOT / "cli/session.py").read_text(encoding="utf-8")
        cli_container = (ROOT / "cli/container.py").read_text(encoding="utf-8")
        client_text = "\n".join((js_text, cli_session, cli_container))
        for endpoint, result in CLIENT_ENDPOINTS.items():
            with self.subTest(endpoint=endpoint, result=result):
                self.assertIn(endpoint, client_text)

        send_source = _function_source("cli/session.py", "send")
        self.assertIn('http.post(\n        "/type"', send_source)
        self.assertNotRegex(send_source, r"subprocess|tmux_send|send_keys")

        actions = (ROOT / "js/actions.js").read_text(encoding="utf-8")
        terminal_js = (ROOT / "js/terminal.js").read_text(encoding="utf-8")
        self.assertRegex(
            actions,
            r"function restartClaudeSession[\s\S]+fetch\('/type/client-restart'",
        )
        self.assertRegex(
            terminal_js,
            r"function resumeSession[\s\S]+fetch\('/type/client-resume'",
        )
        self.assertIn("expected_target_identity: _termExpectedIdentity", actions)
        self.assertIn(
            "expected_target_identity: launchData.expected_target_identity",
            terminal_js,
        )

    def test_background_callbacks_and_ctrl_l_sites_are_closed(self):
        all_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in [ROOT / "serve.py", *sorted((ROOT / "routes").glob("*.py"))]
        )
        for qualified in BACKGROUND_CALLBACKS:
            _relative, callback = qualified.split(":", 1)
            with self.subTest(callback=callback):
                self.assertIn(callback, all_source)

        streaming = (ROOT / "routes/streaming.py").read_text(encoding="utf-8")
        terminal = (ROOT / "routes/terminal.py").read_text(encoding="utf-8")
        self.assertNotIn("target=_force_redraw", streaming)
        self.assertNotIn('["tmux", "send-keys", "-t", exact, "C-l"]', streaming)
        self.assertNotIn('"send-keys", "-t", tmux_exact_target(session), "C-l"', terminal)
        self.assertNotIn('"send-keys", "-t", tmux_exact_target(target), "C-l"', terminal)

if __name__ == "__main__":
    unittest.main()
