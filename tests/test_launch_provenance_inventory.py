import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CREATE_TOKEN = re.compile(r"(?P<quote>['\"])(new-session|split-window)(?P=quote)")


class LaunchProvenanceInventoryTests(unittest.TestCase):
    def test_no_production_survivor_or_container_origin_path(self):
        production = "\n".join(
            path.read_text(encoding="utf-8") for path in self._production_python()
        )
        self.assertNotIn('"pre_boundary_survivor"', production)
        provenance_source = (ROOT / "shared" / "launch_provenance.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('"resource_kind": "container"', provenance_source)
        observer = (ROOT / "shared" / "park_activation.py").read_text(encoding="utf-8")
        self.assertIn('"classification": "ambient_unowned"', observer)
    @staticmethod
    def _production_python():
        paths = [ROOT / "serve.py"]
        for directory in ("cli", "routes", "shared"):
            paths.extend(sorted((ROOT / directory).glob("*.py")))
        return paths

    def test_all_tmux_creates_and_adoptions_are_classified(self):
        bypasses = []
        for path in self._production_python():
            if path == ROOT / "shared" / "tmux.py":
                continue
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                match = CREATE_TOKEN.search(line)
                if match:
                    bypasses.append(
                        f"{path.relative_to(ROOT)}:{line_number}:{match.group(2)}"
                    )
        self.assertEqual(
            bypasses,
            [],
            "direct production tmux creation bypasses provenance helper:\n"
            + "\n".join(bypasses),
        )
        routed = {
            "fresh_terminal",
            "duplicate",
            "saved_command_split",
            "temporary_git",
            "automate_start",
            "automate_hard_relaunch",
        }
        production = "\n".join(
            path.read_text(encoding="utf-8")
            for path in self._production_python()
            if path
            not in {
                ROOT / "shared" / "tmux.py",
                ROOT / "shared" / "launch_provenance.py",
            }
        )
        for surface in routed:
            self.assertIn(f'surface="{surface}"', production)
        for surface in (
            "existing_terminal",
            "automate_reconnect",
            "automate_startup_recovery",
        ):
            self.assertIn(f'surface="{surface}"', production)
        self.assertNotIn("record_created(", production)

        helper = (ROOT / "shared" / "tmux.py").read_text(encoding="utf-8")
        body = helper[helper.index("def _create_tmux_resource"):helper.index("def create_tmux_session")]
        self.assertLess(body.index("store.locked()"), body.index("subprocess.run("))
        self.assertLess(body.index("subprocess.run("), body.index("_created_identity("))
        self.assertLess(body.index("_created_identity("), body.index("registry.record_created("))
        self.assertLess(body.index("registry.record_created("), body.index('"created"'))
