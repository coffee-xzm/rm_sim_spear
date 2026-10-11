"""Offline tests for the ``tools/run_spear_sim.py`` command line.

Only the parser is exercised: ``main()`` would launch Unreal.  That is why the
tool exposes ``build_parser()`` separately.
"""

import importlib.util
import os
from pathlib import Path
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPO_ROOT / "tools" / "run_spear_sim.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("rm_sim_run_spear_sim", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MujocoViewerDefaultTest(unittest.TestCase):
    """The viewer comes up with the simulation; the opt-out is ``--no-``.

    run_spear_sim.py is the interactive entry point, and the MuJoCo window is
    the only place the collision model is visible (the UE mirror draws visual
    meshes).  So the default is the documented behaviour, and flipping it back
    to opt-in has to break this test rather than silently close the window.
    Headless callers -- the agent iteration path -- must opt out explicitly.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = _load_tool().build_parser()

    def test_viewer_is_on_by_default(self) -> None:
        args = self.parser.parse_args(["--user-config", "x.yaml"])
        self.assertTrue(args.mujoco_viewer)

    def test_no_mujoco_viewer_is_the_opt_out(self) -> None:
        args = self.parser.parse_args(
            ["--user-config", "x.yaml", "--no-mujoco-viewer"])
        self.assertFalse(args.mujoco_viewer)

    def test_both_spellings_are_documented_in_help(self) -> None:
        # A headless caller only learns about the opt-out from --help, so the
        # negative spelling has to be printed there.
        help_text = self.parser.format_help()
        self.assertIn("--mujoco-viewer", help_text)
        self.assertIn("--no-mujoco-viewer", help_text)


class ViewerPreflightTest(unittest.TestCase):
    """The display check that has to run *before* launch_passive.

    Measured 2026-10-10: with no DISPLAY, ``launch_passive`` does not raise
    anything a Python caller can catch -- MuJoCo's FatalError handler prints
    ``ERROR: could not initialize GLFW`` and exits the process from C.  So the
    tool must decide beforehand, and this is that decision.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = _load_tool()

    def test_missing_display_is_reported(self) -> None:
        blank = {k: v for k, v in os.environ.items()
                 if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        with mock.patch.dict(os.environ, blank, clear=True):
            reason = self.tool._viewer_display_reason()
        self.assertIsNotNone(reason)
        self.assertIn("DISPLAY", reason)

    def test_a_set_display_passes_the_preflight(self) -> None:
        # Either variable is enough, and the check must stay a pure read: it
        # must not try to open a window to find out.
        for name in ("DISPLAY", "WAYLAND_DISPLAY"):
            with mock.patch.dict(os.environ, {name: ":1"}, clear=True):
                self.assertIsNone(self.tool._viewer_display_reason())


if __name__ == "__main__":
    unittest.main()


class ArenaCliTest(unittest.TestCase):
    """There is exactly one arena, and it is opt-in by path.

    The ``--arena`` / ``--arena-dir`` / ``--arena-visual`` trio is gone along
    with ``pipeline/arena_scene.py``.  It is pinned as *absent* rather than
    merely untested: a stale flag that still parses would silently select the
    world that was deleted, and argparse's default for a removed option is not
    an error -- it is an AttributeError several hundred lines later.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = _load_tool().build_parser()

    def test_the_removed_arena_flags_are_really_gone(self) -> None:
        help_text = self.parser.format_help()
        for spelling in ("--arena ", "--arena-dir", "--arena-visual"):
            self.assertNotIn(spelling, help_text)
        for spelling in ("--arena", "--no-arena", "--arena-dir", "--arena-visual"):
            with self.assertRaises(SystemExit, msg=spelling):
                with mock.patch("sys.stderr"):
                    self.parser.parse_args(["--user-config", "x.yaml", spelling])

    def test_the_arena_is_opt_in_and_has_no_default(self) -> None:
        args = self.parser.parse_args(["--user-config", "x.yaml"])
        self.assertIsNone(args.arena_scene)
        self.assertIsNone(args.world_offset_cm)


class ArenaSceneOptionsTest(unittest.TestCase):
    """The addon-MJCF arena, at the command-line surface.

    The defaults here are load-bearing and counter-intuitive, so they are pinned
    rather than left to the help text: ``--arena-scene-mirror-y`` is OFF and
    ``--arena-scene-spawn-m`` has no default (the export carries no spawn).

    Mirroring defaults off because the addon's file is *already* in the frame
    the pose bridge wants.  The file is reflected in y relative to Unreal, but
    so is :func:`to_unreal_pose`; mirroring on import would negate y twice, and
    that is what put the arena 3.2 m from where Unreal draws it.  See
    ``docs/rmuc26_asset_review.md``.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = _load_tool().build_parser()

    def test_mirror_is_off_by_default_and_can_be_turned_on(self) -> None:
        args = self.parser.parse_args(["--user-config", "x.yaml"])
        self.assertFalse(args.arena_scene_mirror_y)
        args = self.parser.parse_args(
            ["--user-config", "x.yaml", "--arena-scene-mirror-y"])
        self.assertTrue(args.arena_scene_mirror_y)

    def test_drop_is_repeatable_and_empty_by_default(self) -> None:
        args = self.parser.parse_args(["--user-config", "x.yaml"])
        self.assertEqual(args.arena_scene_drop, [])
        args = self.parser.parse_args(
            ["--user-config", "x.yaml",
             "--arena-scene-drop", "a", "--arena-scene-drop", "b"])
        self.assertEqual(args.arena_scene_drop, ["a", "b"])

    def test_spawn_has_no_default_because_the_export_has_none(self) -> None:
        args = self.parser.parse_args(["--user-config", "x.yaml"])
        self.assertIsNone(args.arena_scene_spawn_m)
        args = self.parser.parse_args(
            ["--user-config", "x.yaml", "--arena-scene-spawn-m", "1", "2", "3"])
        self.assertEqual(args.arena_scene_spawn_m, [1.0, 2.0, 3.0])

    def test_scene_is_opt_in(self) -> None:
        args = self.parser.parse_args(["--user-config", "x.yaml"])
        self.assertIsNone(args.arena_scene)
