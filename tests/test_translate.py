"""Run with: python3 -m unittest discover tests

The build test needs solidcraft-cli (on PATH, or SOLIDCRAFT_CLI); it is skipped without it."""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ExportToSolidCraft"))

import build  # noqa: E402
import translate  # noqa: E402

DEMO = os.path.join(ROOT, "examples", "Demo-box.fusion.json")


class Expressions(unittest.TestCase):
    def test_feet_and_inches(self):
        self.assertEqual(translate.feet_inches("0'- 1/4\""), "(0' + 1/4\")")
        self.assertEqual(translate.feet_inches("1'-6 1/2\""), "(1' + 6 1/2\")")
        self.assertEqual(translate.feet_inches("width - 2 in"), "width - 2 in")
        # names are left alone
        self.assertEqual(translate.feet_inches({"name": "1'-2\""}), {"name": "1'-2\""})

    def test_zero(self):
        for e in (None, "0 in", "0.0 mm", "0"):
            self.assertTrue(translate.is_zero(e), e)
        self.assertFalse(translate.is_zero("2 mm"))


class Translate(unittest.TestCase):
    def test_demo_commands(self):
        data = json.load(open(DEMO))
        t = translate.Translator(data)
        cmds = t.run()
        kinds = [c["command"] for c in cmds]
        self.assertEqual(kinds.count("solid.extrude"), 4)
        self.assertEqual(kinds.count("solid.shell"), 1)
        self.assertNotIn("fusion.fallback", kinds)
        self.assertEqual(t.notes, [])

    def test_half_turn_axis(self):
        t = translate.Translator({"document": "d", "parameters": [], "steps": [], "occurrences": []})
        # 180 degrees about (1, 1, 0)
        t.move(["b"], [0, 1, 0, 0, 1, 0, 0, 0, 0, 0, -1, 0, 0, 0, 0, 1], [])
        axis = t.out[-1]["params"]["axis"]
        self.assertAlmostEqual(abs(axis[0]), abs(axis[1]))
        self.assertGreater(axis[0] * axis[1], 0)


@unittest.skipUnless(os.environ.get("SOLIDCRAFT_CLI") or shutil.which("solidcraft-cli"), "needs solidcraft-cli")
class Build(unittest.TestCase):
    def test_demo_matches_fusion(self):
        data = json.load(open(DEMO))
        with tempfile.TemporaryDirectory() as d:
            report = build.build(data, os.path.join(d, "demo.solidcraft"), build.DEFAULT_CLI)
        text = "\n".join(report)
        self.assertIn("0 failed", text)
        self.assertIn("all match Fusion", text)


if __name__ == "__main__":
    unittest.main()
