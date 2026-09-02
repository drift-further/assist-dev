"""The first-party execution boundary for parked container-launch automation.

Callers submit trusted provenance as an :class:`Intent` together with the
complete effect closure.  The decision and the effect linearize under one
exclusive lock; this module deliberately exposes no check/permit API.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable, TypeVar


SCHEMA = "assist-execution-park-v1"
POLICY_NAME = "container_launch_park"
REFUSAL_ERROR = "container_launch_parked"
REFUSAL_REASON = (
    "Container launch automation is temporarily parked while host wiring migrates."
)


class Phase(str, Enum):
    DRAINING = "draining"
    PARKED = "parked"


class Intent(str, Enum):
    AUTOMATE_START = "automate_start"
    AUTOMATE_HARD_RELAUNCH = "automate_hard_relaunch"
    AUTOMATE_SOFT_CLEAR = "automate_soft_clear"
    AUTOMATE_SOFT_RESEND = "automate_soft_resend"
    AUTOMATE_TRUST_ANSWER = "automate_trust_answer"
    AUTOMATE_AUTO_ANSWER = "automate_auto_answer"
    TERMINAL_INIT_LAUNCH = "terminal_init_launch"
    TERMINAL_INIT_DUPLICATE = "terminal_init_duplicate"
    TERMINAL_RUN_INIT = "terminal_run_init"
    CONFIGURED_RESTART = "configured_restart"
    SAVED_COMMAND = "saved_command"
    PROJECT_VENV = "project_venv"
    FIXED_GIT = "fixed_git"
    CONFIGURED_CLI_PROXY = "configured_cli_proxy"
    CONFIGURED_IMAGE_BUILD = "configured_image_build"
    NATIVE_FOLDER_PICKER = "native_folder_picker"
    AUTOYES_ANSWER = "autoyes_answer"
    OPERATOR_INTERACTIVE = "operator_interactive"
    CLIENT_SESSION_RESUME = "client_session_resume"
    CLIENT_SESSION_RESTART = "client_session_restart"
    BARE_TERMINAL = "bare_terminal"
    OBSERVE = "observe"
    STOP = "stop"
    IMAGE_CONFIG = "image_config"


DENIED_INTENTS = frozenset(
    {
        Intent.AUTOMATE_START,
        Intent.AUTOMATE_HARD_RELAUNCH,
        Intent.AUTOMATE_SOFT_CLEAR,
        Intent.AUTOMATE_SOFT_RESEND,
        Intent.AUTOMATE_TRUST_ANSWER,
        Intent.AUTOMATE_AUTO_ANSWER,
        Intent.CONFIGURED_CLI_PROXY,
        Intent.CONFIGURED_IMAGE_BUILD,
    }
)

ALLOWED_INTENTS = frozenset(Intent) - DENIED_INTENTS


@dataclass(frozen=True, slots=True)
class Refusal:
    """The one canonical HTTP/background refusal value."""

    intent: Intent
    schema: str = SCHEMA
    policy: str = POLICY_NAME
    error: str = REFUSAL_ERROR
    reason: str = REFUSAL_REASON
    http_status: int = 409

    def body(self) -> dict[str, object]:
        return {
            "ok": False,
            "error": self.error,
            "reason": self.reason,
            "intent": self.intent.value,
        }

    def background(self) -> dict[str, object]:
        return {
            "status": self.error,
            "reason": self.reason,
            "intent": self.intent.value,
        }


EffectResult = TypeVar("EffectResult")


class ExecutionPark:
    """Linearized policy instance for one Assist server process."""

    def __init__(self, phase: Phase = Phase.DRAINING):
        self._decision_lock = threading.Lock()
        self._phase = Phase(phase)
        self._activation_path = None
        self._unresolved_units = []

    @property
    def phase(self) -> Phase:
        with self._decision_lock:
            return self._phase

    def publish_parked(self) -> None:
        """Publish the sole forward activation transition under the decision lock."""
        with self._decision_lock:
            if self._phase is not Phase.DRAINING:
                raise RuntimeError("execution park is already parked")
            self._phase = Phase.PARKED
            self._unresolved_units = []

    def configure_activation(self, activation_path: str, units) -> None:
        """Publish observer identities, not a capability or reusable permit."""
        with self._decision_lock:
            self._activation_path = str(activation_path)
            self._unresolved_units = [
                dict(unit)
                for unit in units
                if unit.get("outcome") != "terminal"
            ]

    def activation_status(self) -> dict[str, object]:
        with self._decision_lock:
            return {
                "park_phase": self._phase.value,
                "activation_path": self._activation_path,
                "unresolved_units": [dict(unit) for unit in self._unresolved_units],
            }

    def perform(
        self, intent: Intent, effect: Callable[[], EffectResult]
    ) -> EffectResult | Refusal:
        """Classify provenance and either refuse or finish the effect atomically."""
        if not isinstance(intent, Intent):
            raise TypeError("execution intent must be an Intent enum member")
        if not callable(effect):
            raise TypeError("execution effect must be callable")
        with self._decision_lock:
            if intent in DENIED_INTENTS:
                return Refusal(intent)
            return effect()


_PROCESS_PARK = ExecutionPark()


def perform(
    intent: Intent, effect: Callable[[], EffectResult]
) -> EffectResult | Refusal:
    """Run one whole first-party effect through the process park."""
    return _PROCESS_PARK.perform(intent, effect)


def phase() -> Phase:
    return _PROCESS_PARK.phase


def publish_parked() -> None:
    _PROCESS_PARK.publish_parked()


def configure_activation(activation_path: str, units) -> None:
    _PROCESS_PARK.configure_activation(activation_path, units)


def activation_status() -> dict[str, object]:
    return _PROCESS_PARK.activation_status()


def is_refusal(value: object) -> bool:
    return isinstance(value, Refusal)
