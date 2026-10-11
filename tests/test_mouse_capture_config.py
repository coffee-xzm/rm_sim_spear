"""Regression guard: the project must not re-enable UE's pointer capture.

The two files below are what stop a `-game` window or PIE from grabbing the
desktop pointer (see docs/agents.user.md).  Both are plain config, so nothing
else fails loudly if one is reverted -- hence this test.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_INPUT_SETTINGS = {
    "bCaptureMouseOnLaunch": "False",
    "DefaultViewportMouseCaptureMode": "NoCapture",
    "DefaultViewportMouseLockMode": "DoNotLock",
}
EXPECTED_LAUNCH_ARGS = {
    "norelativemousemode": "null",
    "DefaultViewportMouseCaptureMode": "NoCapture",
}


def _ini_values(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "#", "[")):
            continue
        key, sep, value = stripped.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


class MouseCaptureConfigTests(unittest.TestCase):
    def test_default_input_ini_disables_viewport_mouse_capture(self):
        values = _ini_values(REPO_ROOT / "ue/Config/DefaultInput.ini")
        for key, expected in EXPECTED_INPUT_SETTINGS.items():
            self.assertIn(key, values)
            self.assertEqual(values[key], expected, f"{key} must stay {expected}")

    def test_spear_user_config_keeps_the_command_line_overrides(self):
        import yaml

        config = yaml.safe_load(
            (REPO_ROOT / "tools/spear_user_config.yaml").read_text(encoding="utf-8"))
        args = config["SPEAR"]["INSTANCE"]["COMMAND_LINE_ARGS"]
        for key, expected in EXPECTED_LAUNCH_ARGS.items():
            self.assertIn(key, args)
            if expected == "null":
                self.assertIsNone(args[key], f"{key} must stay null")
            else:
                self.assertEqual(args[key], expected, f"{key} must stay {expected}")


if __name__ == "__main__":
    unittest.main()
