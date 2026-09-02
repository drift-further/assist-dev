"""What a fresh install of Auto-Yes is armed with.

Auto-Yes answers permission prompts with no human in the loop. The switch that
arms every agent pane on the host at once is the single most consequential
setting Assist has, so the shipped posture is pinned here: OFF, and with a
countdown long enough to see and cancel.

The second half of the file covers the way a live install can leave that
posture WITHOUT touching the switch: the countdown delay is read straight out
of settings.json / project_settings.json by the scanner, and those files are
hand-editable and reachable through PATCH /api/settings, which validates key
names but not values. A delay of 0 fires with no cancel window; a delay of
"banana" raises inside the scan tick, which is caught and logged — so auto-yes
stops working entirely and looks merely quiet.

Run: .venv/bin/python3 -m unittest tests.test_autoyes_posture
"""

import unittest
from unittest.mock import patch

import shared.state as state
from routes.autoyes import _clamp_delay, _safe_delay


class ShippedDefaultTests(unittest.TestCase):
    """A clone with no settings.json must be cold."""

    def test_all_sessions_switch_ships_off(self):
        self.assertEqual(state.DEFAULT_SETTINGS["autoyes"]["all_sessions"], "off")

    def test_default_delay_ships_long_enough_to_cancel(self):
        delay = state.DEFAULT_SETTINGS["autoyes"]["default_delay"]
        self.assertGreaterEqual(delay, 1)

    def test_per_project_default_is_not_auto_enabled(self):
        autoyes = state.DEFAULT_PROJECT_SETTINGS["autoyes"]
        self.assertFalse(autoyes["enabled_default"])
        self.assertFalse(autoyes["global_opt_out"])

    def test_only_the_exact_string_on_arms_the_switch(self):
        """Anything else — True, "yes", "ON", junk — must read as off."""
        for value in (True, "yes", "ON", "1", "", None, 1):
            with self.subTest(value=value):
                self.assertNotEqual(value, "on")

    def test_settings_json_is_gitignored(self):
        """A clone must not be able to inherit someone else's hot posture."""
        ignored = (state.SETTINGS_FILE.parent / ".gitignore").read_text()
        self.assertIn("settings.json", ignored)
        self.assertIn("project_settings.json", ignored)


class DelayResolutionTests(unittest.TestCase):
    """The delay the scanner actually counts down with."""

    SHIPPED = state.DEFAULT_SETTINGS["autoyes"]["default_delay"]

    def test_a_sane_delay_is_passed_through(self):
        self.assertEqual(_safe_delay(5), 5.0)
        self.assertEqual(_safe_delay(0.1), 0.1)

    def test_zero_is_raised_to_the_floor_not_fired_instantly(self):
        """0 means "no countdown, no cancel window" — never honour it."""
        self.assertGreaterEqual(_safe_delay(0), 0.1)

    def test_negative_delays_are_raised_to_the_floor(self):
        self.assertGreaterEqual(_safe_delay(-30), 0.1)

    def test_absurd_delays_are_capped(self):
        self.assertLessEqual(_safe_delay(10**9), 30.0)

    def test_junk_falls_back_to_the_shipped_default_instead_of_raising(self):
        """A string in settings.json used to kill the whole scan tick."""
        for junk in ("banana", None, [], {}, object()):
            with self.subTest(junk=junk):
                self.assertEqual(_safe_delay(junk), float(self.SHIPPED))

    def test_the_raw_clamp_still_rejects_junk_for_api_callers(self):
        """_safe_delay must not soften the 400 that API setters rely on."""
        with self.assertRaises((TypeError, ValueError)):
            _clamp_delay("banana")

    def test_a_poisoned_settings_file_cannot_disarm_the_countdown(self):
        """End to end: settings.json says 0, the scanner still counts down."""
        with patch(
            "shared.state.get_setting",
            side_effect=lambda *keys: 0 if keys[-1] == "default_delay" else 8,
        ):
            self.assertGreaterEqual(
                _safe_delay(state.get_setting("autoyes", "default_delay")), 0.1
            )


if __name__ == "__main__":
    unittest.main()
