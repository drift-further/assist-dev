"""The server grammar must keep browser-vault tokens visible and inert.

The dollar sigil is the fail-safe: if client JavaScript is stale, disabled, or
cannot read browser storage, ``[$handle]`` reaches the pane literally. It must
never become a server segment, even through a hand-edited favorites file.

Run: .venv/bin/python3 -m unittest tests.test_vault_grammar
"""

import unittest
from unittest.mock import patch

from flask import Flask

from routes.input import input_bp
from shared import segments
from shared.tmux import DeliveryResult, ExpectedTargetIdentity


def _expected_identity():
    return ExpectedTargetIdentity(
        socket_path="/tmp/assist-test-tmux.sock",
        socket_device=1,
        socket_inode=2,
        server_pid=3,
        server_start_time="4",
        session_id="$1",
        window_id="@1",
        pane_id="%1",
        pane_pid=5,
        pane_start_time="6",
    )


class VaultServerGrammarTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.logger.disabled = True
        app.register_blueprint(input_bp)
        self.client = app.test_client()

    def test_token_regex_cannot_match_a_vault_token(self):
        self.assertIsNone(segments.TOKEN_RE.search("[$sudo]"))

    def test_vault_handle_is_not_a_valid_server_handle(self):
        self.assertFalse(segments.valid_handle("$sudo"))

    def test_hand_edited_favorite_cannot_claim_a_vault_handle(self):
        favs = [
            {"id": "f_bad", "handle": "$sudo", "text": "must-not-expand"},
            {"id": "f_ok", "handle": "ordinary", "text": "expanded"},
        ]
        self.assertEqual(segments.segment_map(favs), {"ordinary": "expanded"})

    def test_expand_leaves_vault_token_byte_for_byte(self):
        self.assertEqual(
            segments.expand("before [$sudo] after", {"sudo": "must-not-expand"}),
            "before [$sudo] after",
        )

    @patch(
        "routes.input._load_favorites",
        return_value=[{"id": "f_bad", "handle": "$sudo", "text": "hidden"}],
    )
    def test_preview_returns_literal_token_and_no_server_tokens(self, _load):
        response = self.client.post("/segments/expand", json={"text": "[$sudo]"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["expanded"], "[$sudo]")
        self.assertEqual(response.get_json()["tokens"], [])

    def test_secret_type_is_exact_and_skips_every_text_convenience(self):
        raw = "  Git [ordinary] \\[literal]  "
        expected = _expected_identity()
        with patch(
            "routes.input.expected_target_identity", return_value=expected
        ), patch(
            "routes.input.generation_bound_delivery",
            return_value=DeliveryResult("delivered"),
        ) as deliver, patch(
            "routes.input.pane_awaits_secret"
        ) as pane_secret, patch("routes.input.fix_first_word_case") as fix_case, patch(
            "routes.input.segments.expand"
        ) as expand, patch("routes.input.add_to_history") as history, patch(
            "routes.input.declare_agent_command"
        ) as declare, patch("routes.input.state.touch_activity"):
            response = self.client.post(
                "/type",
                json={
                    "text": raw,
                    "enter": True,
                    "expand": True,
                    "secret": True,
                    "target": "vault-test:0.0",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["sent_chars"], len(raw))
        deliver.assert_called_once_with(expected, text=raw, enter=True)
        pane_secret.assert_not_called()
        fix_case.assert_not_called()
        expand.assert_not_called()
        history.assert_not_called()
        declare.assert_not_called()

    @patch("routes.input._load_favorites", return_value=[])
    def test_unresolved_vault_token_reaches_tmux_literally(self, _load):
        expected = _expected_identity()
        with patch(
            "routes.input.expected_target_identity", return_value=expected
        ), patch(
            "routes.input.generation_bound_delivery",
            return_value=DeliveryResult("delivered"),
        ) as deliver, patch(
            "routes.input.pane_awaits_secret", return_value=False
        ), patch("routes.input.add_to_history") as history, patch(
            "routes.input.declare_agent_command"
        ), patch("routes.input.state.touch_activity"):
            response = self.client.post(
                "/type",
                json={
                    "text": "[$sudo]",
                    "enter": True,
                    "expand": True,
                    "target": "vault-test:0.0",
                },
            )

        self.assertEqual(response.status_code, 200)
        deliver.assert_called_once_with(expected, text="[$sudo]", enter=True)
        history.assert_called_once_with("[$sudo]")


if __name__ == "__main__":
    unittest.main()
