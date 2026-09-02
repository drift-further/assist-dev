"""Starring a history entry must create a favorite, and a second star must remove it.

The star on a Prompts/Cmds row POSTs /favorite with the entry's text. The add
branch stamps `ts` via time.strftime; when the execution-park refactor (86707e3)
dropped `import time` from routes/input.py, every star tap 500'd while unstar —
which never reaches that line — kept working. A one-directional break with no
visible error on the phone, which is exactly the kind this suite exists to pin.

Run: .venv/bin/python3 -m unittest tests.test_favorites
"""

import json
import inspect
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import shared.state as state
import shared.utils as utils
from routes import input as input_routes


class FavoriteStarTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.logger.disabled = True
        app.register_blueprint(input_routes.input_bp)
        self.app = app
        self.client = app.test_client()

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fav_file = Path(tmp.name) / "favorites.json"
        self.hist_file = Path(tmp.name) / "history.json"
        for name, path in (("FAVORITES_FILE", self.fav_file), ("HISTORY_FILE", self.hist_file)):
            patcher = patch.object(state, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _favorites_on_disk(self):
        return json.loads(self.fav_file.read_text())

    def test_star_adds_a_favorite(self):
        response = self.client.post("/favorite", json={"text": "run the tests"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "action": "added"})
        favs = self._favorites_on_disk()
        self.assertEqual([f["text"] for f in favs], ["run the tests"])
        self.assertTrue(favs[0]["id"].startswith("f_"))
        self.assertRegex(favs[0]["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

    def test_new_favorite_lands_first_and_shows_in_history(self):
        self.client.post("/favorite", json={"text": "older"})
        self.client.post("/favorite", json={"text": "newer"})

        listed = self.client.get("/history").get_json()["favorites"]
        self.assertEqual([f["text"] for f in listed], ["newer", "older"])

    def test_second_star_removes_it(self):
        self.client.post("/favorite", json={"text": "run the tests"})
        response = self.client.post("/favorite", json={"text": "run the tests"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "action": "removed"})
        self.assertEqual(self._favorites_on_disk(), [])

    def test_bare_star_keeps_a_named_segment(self):
        self.fav_file.write_text(json.dumps(
            [{"id": "f_seg", "handle": "realign", "text": "standing brief"}]
        ))

        response = self.client.post("/favorite", json={"text": "standing brief"})

        self.assertEqual(response.get_json()["action"], "kept")
        self.assertEqual(response.get_json()["id"], "f_seg")
        self.assertEqual(len(self._favorites_on_disk()), 1)

    def test_empty_text_is_rejected(self):
        self.assertEqual(self.client.post("/favorite", json={"text": "  "}).status_code, 400)

    def test_concurrent_stars_do_not_lose_either_favorite(self):
        real_load = input_routes.load_json
        first_read = threading.Event()
        second_read = threading.Event()
        order_lock = threading.Lock()
        reads = 0

        def coordinated_load(path, default=None):
            nonlocal reads
            value = real_load(path, default=default)
            if Path(path) != self.fav_file:
                return value
            with order_lock:
                order = reads
                reads += 1
            if order == 0:
                first_read.set()
                second_read.wait(0.2)
            elif order == 1:
                second_read.set()
            return value

        statuses = []

        def star(text):
            with self.app.test_client() as client:
                statuses.append(client.post("/favorite", json={"text": text}).status_code)

        with patch("routes.input.load_json", side_effect=coordinated_load):
            first = threading.Thread(target=star, args=("first",))
            second = threading.Thread(target=star, args=("second",))
            first.start()
            self.assertTrue(first_read.wait(1))
            second.start()
            first.join(2)
            second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(sorted(statuses), [200, 200])
        self.assertEqual(
            {favorite["text"] for favorite in self._favorites_on_disk()},
            {"first", "second"},
        )

    def test_all_favorite_mutations_hold_the_shared_lock(self):
        for function in (
            input_routes.favorite,
            input_routes.update_favorite,
            input_routes.delete_favorite,
        ):
            with self.subTest(function=function.__name__):
                self.assertIn("with _favorites_lock", inspect.getsource(function))

    def test_clear_history_uses_the_history_mutation_lock(self):
        self.hist_file.write_text('[{"text":"keep until locked"}]', encoding="utf-8")
        started = threading.Event()
        finished = threading.Event()
        statuses = []

        def clear():
            started.set()
            with self.app.test_client() as client:
                statuses.append(client.delete("/history").status_code)
            finished.set()

        utils._history_lock.acquire()
        try:
            worker = threading.Thread(target=clear)
            worker.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(0.1))
        finally:
            utils._history_lock.release()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(statuses, [200])
        self.assertEqual(json.loads(self.hist_file.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
