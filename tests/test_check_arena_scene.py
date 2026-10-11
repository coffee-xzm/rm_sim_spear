"""The frame check for an addon-exported arena scene.

``tools/check_arena_scene.py`` answers a question that cannot be read off the
file -- which transform brings the scene into level coordinates -- and it got
that answer wrong once, in a way that is worth a test rather than a comment.

The defect was that the tool solved for ``--world-offset-cm`` with a formula of
its own instead of using :func:`to_unreal_pose`.  The two disagreed about the
sign of y (the bridge negates y, the formula did not), so the tool recommended
an offset and a mirror flag that both had the wrong sign, and the arena was
placed 3.2 m from where Unreal draws it.  These tests therefore do not assert
"mirror" or "no mirror"; they assert the only thing that actually matters:
**feeding the recommendation back through the bridge lands on the level.**
"""

import importlib.util
from pathlib import Path
import tempfile
import unittest

import mujoco
import numpy as np

from rm_sim_spear.model import Pose
from rm_sim_spear.mujoco_driver import PhysicsOptions
from rm_sim_spear.pose_sync import to_unreal_pose

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPO_ROOT / "tools" / "check_arena_scene.py"

CUBE_OBJ = """\
v -0.5 -0.5 -0.5
v 0.5 -0.5 -0.5
v 0.5 0.5 -0.5
v -0.5 0.5 -0.5
v -0.5 -0.5 0.5
v 0.5 -0.5 0.5
v 0.5 0.5 0.5
v -0.5 0.5 0.5
f 1 4 3
f 1 3 2
f 5 6 7
f 5 7 8
f 1 2 6
f 1 6 5
f 3 4 8
f 3 8 7
f 4 1 5
f 4 5 8
f 2 3 7
f 2 7 6
"""

#: Two objects at different places, so a wrong sign cannot cancel out.
SCENE = """\
<mujoco model="synthetic addon export">
  <include file="robot.xml"/>
  <asset>
    <mesh name="box" file="cube.obj"/>
  </asset>
  <worldbody>
    <light pos="0 0 1.5" dir="0 0 -1"/>
    <geom name="floor" size="0 0 0.05" type="plane"/>
    <body name="alpha" pos="1 2 0">
      <geom name="hull" type="mesh" mesh="box" density="0" group="3"/>
    </body>
    <body name="beta" pos="-3 -4 1">
      <geom name="hull_b" type="mesh" mesh="box" density="0" group="3"/>
    </body>
  </worldbody>
</mujoco>
"""


def _load_tool():
    spec = importlib.util.spec_from_file_location("rm_sim_check_arena_scene",
                                                  TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bridge(point_m, offset_cm) -> np.ndarray:
    """The real MuJoCo -> Unreal conversion, offset included."""
    pose = Pose(position_m=tuple(float(v) for v in point_m),
                quaternion_wxyz=(1.0, 0.0, 0.0, 0.0))
    return np.asarray(to_unreal_pose(pose, world_offset_cm=offset_cm).location_cm,
                      float)


class SpawnRoundTrip(unittest.TestCase):
    """The spawn the tool prints must survive the bridge.

    ``--arena-scene-spawn-m`` is given in the *scene's* frame, and the bridge
    turns scene coordinates into Unreal ones.  If the tool uses any convention
    other than the bridge's, the robot spawns somewhere else in Unreal -- which
    is exactly the reported symptom, and which nothing else in the suite covers
    because both halves are individually correct.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = _load_tool()

    def test_the_printed_spawn_maps_onto_the_level_spawn(self) -> None:
        for offset in ([-1650.5, 162.43, 0.0], [0.0, 0.0, 0.0],
                       [120.0, -340.0, 55.0]):
            for spawn_m in ([0.0, 0.0, -1.3765], [3.0, -2.0, 0.5]):
                # Exactly what main() prints, from a level spawn in metres.
                scene_spawn = np.array([1.0, -1.0, 1.0]) * (
                    np.asarray(spawn_m, float) - np.asarray(offset, float) / 100.0)
                landed = _bridge(scene_spawn, offset)
                np.testing.assert_allclose(landed, 100.0 * np.asarray(spawn_m),
                                           atol=1e-6)

    def test_the_unchanged_y_formula_would_not_round_trip(self) -> None:
        """Pin the defect itself, so the fix cannot be quietly reverted.

        Carrying the sign of y across unchanged -- the old behaviour -- misses
        by twice the offset, which is where the 3.2 m came from.
        """
        offset = np.array([-1650.5, 162.43, 0.0])
        spawn_m = np.array([0.0, 0.0, -1.3765])
        wrong = np.ones(3) * (spawn_m - offset / 100.0)
        landed = _bridge(wrong, offset)
        self.assertGreater(abs(landed[1] - 100.0 * spawn_m[1]), 300.0)


class ImpliedOffsets(unittest.TestCase):
    """``implied_offsets`` must invert the bridge, for any level.

    Built round the other way from the rest of the suite: rather than trusting a
    real ``scene.json``, a synthetic level is *generated* by pushing a synthetic
    scene through the bridge at a known offset, and the tool then has to recover
    that offset from actor bounds alone.  If the tool used any convention but the
    bridge's, the recovered y would come back with the wrong sign.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = _load_tool()

    def test_recovers_the_offset_that_generated_the_level(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "cube.obj").write_text(CUBE_OBJ, encoding="utf-8")
            (root / "scene.xml").write_text(SCENE, encoding="utf-8")
            model = PhysicsOptions(
                arena_scene=root / "scene.xml")._arena_scene_base_spec().compile()

            data = mujoco.MjData(model)
            mujoco.mj_forward(model, data)
            centres = self.tool._centres(self.tool.scene_bounds(model, data))
            self.assertGreaterEqual(len(centres), 2)

            for offset_cm in (np.array([-1650.5, 162.43, 0.0]),
                              np.array([0.0, 0.0, 0.0]),
                              np.array([-100.0, -250.0, 30.0])):
                level = {}
                for name, centre in centres.items():
                    actor = name[len("static_"):] if name.startswith("static_") else name
                    got = self.tool.scene_to_unreal_cm(np.asarray(centre)) + offset_cm
                    level[actor] = (got / 100.0, got / 100.0)   # zero-extent AABB

                implied, _ = self.tool.implied_offsets(model, level)
                self.assertEqual(len(implied), len(centres))
                for actor, value in implied.items():
                    np.testing.assert_allclose(value, offset_cm, atol=1e-6,
                                               err_msg=f"{actor} at {offset_cm}")


if __name__ == "__main__":
    unittest.main()
