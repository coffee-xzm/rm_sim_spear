"""Importing a whole MJCF world exported by the Blender terrain addon.

This is the second arena flavour, and the two things that make it different from
``arena.json`` are both silent failures, so they are pinned here:

* the export is in Blender's frame, which is the level **reflected** in y
  relative to Unreal -- but so is :func:`to_unreal_pose`, so the file is
  already in the frame the bridge wants and mirroring on import negates y a
  second time (see ``docs/rmuc26_asset_review.md``);
* the addon approximates every plain "extra collider" by a single convex hull,
  and a hull of a large or concave object covers space the object does not.

A synthetic scene is used rather than ``assets/rmuc26``: the point is the
transform, and a three-geom world makes a wrong sign obvious in the assertion
instead of buried in 157 MB of geometry.
"""

import struct
import tempfile
import unittest
from pathlib import Path

import mujoco
import numpy as np

from rm_sim_spear.arena import resolve_world_offset_cm
from rm_sim_spear.mujoco_driver import (
    ARENA_GROUND_GEOM,
    PhysicsOptions,
)

MODEL = Path(__file__).resolve().parents[1] / "generated" / "urdf" / "总装" / "mujoco.urdf"

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

#: 3x4 grid, values chosen so a row reversal is not a no-op and a column
#: reversal (the wrong axis) is detectable.
FIELD = np.array([[0.0, 0.1, 0.2, 0.3],
                  [1.0, 1.1, 1.2, 1.3],
                  [2.0, 2.1, 2.2, 2.3]], dtype="<f4")

SCENE = """\
<mujoco model="synthetic addon export">
  <include file="robot.xml"/>
  <asset>
    <mesh name="box" file="cube.obj"/>
    <hfield name="field" file="field.hfield" size="1 2 0.5 0.1"/>
  </asset>
  <worldbody>
    <light pos="0 0 1.5" dir="0 0 -1"/>
    <geom name="floor" size="0 0 0.05" type="plane"/>
    <body name="frame" pos="0 5 0">
      <geom name="visual" type="mesh" mesh="box" contype="0" conaffinity="0" group="2"/>
      <geom name="terrain" type="hfield" hfield="field" pos="0 -1 -0.5" group="3"/>
      <body name="part" pos="0 -2 0">
        <geom name="hull" type="mesh" mesh="box" density="0" group="3"/>
      </body>
    </body>
  </worldbody>
</mujoco>
"""


class ArenaSceneImport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "cube.obj").write_text(CUBE_OBJ, encoding="utf-8")
        (self.dir / "scene.xml").write_text(SCENE, encoding="utf-8")
        (self.dir / "field.hfield").write_bytes(
            struct.pack("<ii", *FIELD.shape) + FIELD.tobytes())
        self.scene = self.dir / "scene.xml"

    def tearDown(self):
        self._tmp.cleanup()

    def options(self, **kw) -> PhysicsOptions:
        return PhysicsOptions(arena_scene=self.scene, **kw)

    def test_unresolvable_include_is_satisfied(self):
        """The addon's ``<include file="robot.xml"/>`` seam must not be fatal."""
        spec = self.options()._arena_scene_base_spec()
        self.assertGreaterEqual(len(list(spec.geoms)), 3)

    def test_preview_plane_is_removed(self):
        """The addon's z=0 preview plane is not the level's ground."""
        model = self.options()._arena_scene_base_spec().compile()
        planes = [i for i in range(model.ngeom) if model.geom_type[i] == 0]
        self.assertEqual(planes, [])

    def test_geoms_are_prefixed_and_the_ground_is_named(self):
        model = self.options()._arena_scene_base_spec().compile()
        names = {mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, i)
                 for i in range(model.ngeom)}
        self.assertIn("arena_visual", names)
        self.assertIn("arena_hull", names)
        self.assertIn(ARENA_GROUND_GEOM, names)
        self.assertNotIn("floor", names)
        ground = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, ARENA_GROUND_GEOM)
        self.assertEqual(int(model.geom_type[ground]), int(mujoco.mjtGeom.mjGEOM_HFIELD))
        self.assertEqual(int(model.geom_group[ground]), 0)

    def test_drop_geoms_deletes_and_an_unknown_name_is_an_error(self):
        model = self.options(
            arena_scene_drop_geoms=("hull",))._arena_scene_base_spec().compile()
        names = {mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, i)
                 for i in range(model.ngeom)}
        self.assertNotIn("arena_hull", names)
        with self.assertRaises(ValueError):
            self.options(arena_scene_drop_geoms=("nope",))._arena_scene_base_spec()

    def test_mirror_negates_y_everywhere(self):
        """Bodies, geoms and mesh scale all reflect together, or the level shears."""
        plain = self.options()._arena_scene_base_spec().compile()
        mirror = self.options(arena_scene_mirror_y=True)._arena_scene_base_spec().compile()
        for name in ("arena_visual", "arena_hull", ARENA_GROUND_GEOM):
            self.assertAlmostEqual(float(mirror.geom(name).pos[1]),
                                   -float(plain.geom(name).pos[1]), places=9)
            self.assertAlmostEqual(float(mirror.geom(name).pos[0]),
                                   float(plain.geom(name).pos[0]), places=9)

        def body_y(model, name):
            return float(model.body(name).pos[1])

        self.assertAlmostEqual(body_y(mirror, "frame"), -5.0, places=9)
        self.assertAlmostEqual(body_y(mirror, "part"), 2.0, places=9)

        # A reflection cannot be folded into a quaternion, so the mesh itself
        # carries it; without this the placements would mirror and the shapes
        # would not.
        hull = mujoco.mj_name2id(mirror, mujoco.mjtObj.mjOBJ_GEOM, "arena_hull")
        data_id = int(mirror.geom_dataid[hull])
        self.assertEqual(float(mirror.mesh_scale[data_id][1]), -1.0)

    def test_mirror_row_reverses_the_height_field(self):
        """y runs along the rows, so the reflection is the row order."""
        self.options(arena_scene_mirror_y=True)._arena_scene_base_spec()
        derived = self.dir / "field_mirror_y.hfield"
        self.assertTrue(derived.is_file())
        raw = derived.read_bytes()
        nrow, ncol = struct.unpack("<ii", raw[:8])
        self.assertEqual((nrow, ncol), FIELD.shape)
        got = np.frombuffer(raw[8:], "<f4").reshape(nrow, ncol)
        np.testing.assert_array_equal(got, FIELD[::-1])

    def test_mirror_is_off_by_default(self):
        """The addon's file is already in the frame the pose bridge wants.

        The file *is* reflected in y relative to Unreal, which is why this flag
        exists -- but the bridge applies that same reflection, so defaulting it
        on negated y twice and put the arena 3.2 m from where Unreal draws it.
        """
        self.assertFalse(PhysicsOptions().arena_scene_mirror_y)
        spec = self.options()._arena_scene_base_spec().compile()
        self.assertAlmostEqual(float(spec.body("frame").pos[1]), 5.0, places=9)
        self.assertFalse((self.dir / "field_mirror_y.hfield").exists())

    def test_robot_attaches_into_the_imported_world(self):
        """The whole point: our robot on somebody else's world."""
        options = self.options(
            free_base=True,
            driven_joints=("link_1_joint", "link_2_joint",
                           "link_3_joint", "link_4_joint"),
            simple_collision=True,
            arena_scene_mirror_y=True,
            arena_scene_drop_geoms=("hull",),
            root_pos_m=(0.0, 0.0, 0.5))
        model = options.build_spec(MODEL).compile()
        self.assertIsNotNone(model.body("base_link"))
        names = {mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, i)
                 for i in range(model.ngeom)}
        self.assertIn(ARENA_GROUND_GEOM, names)
        self.assertIn("wheel_link_1_joint", names)


class ArenaPolicyTest(unittest.TestCase):
    """The offset policy both run tools share.

    Moved here from ``test_arena_scene.py`` when ``--arena`` was deleted.  Only
    these two cases were about anything other than ``arena.json``; the rest of
    that file tested the pipeline that is gone.
    """

    def test_no_arena_keeps_the_documented_plane_offset(self) -> None:
        self.assertEqual(resolve_world_offset_cm(None), (0.0, 0.0, -140.0))
        self.assertEqual(resolve_world_offset_cm((1, 2, 3)), (1.0, 2.0, 3.0))

    def test_the_addon_scene_is_the_world_that_needs_the_offset(self) -> None:
        """An exported scene carries its own origin, so the offset is the point.

        Refusing it here (as this did at first) leaves the pose bridge with no
        way to put the MuJoCo world and the Unreal level in the same place: the
        measured export sits 16.5 m along x from level coordinates, so the robot
        lands 16.5 m away and the capture camera rides it there.

        Note the sign of y: it is the *bridge's* offset, and the bridge negates
        y, so this is the opposite sign to the scene's own y offset.  See
        ``test_check_arena_scene.py``.
        """
        scene = Path(__file__).resolve().parents[1] / "assets/rmuc26/mjcf/scene_rmuc26.xml"
        self.assertEqual(resolve_world_offset_cm(None, scene), (0.0, 0.0, 0.0))
        self.assertEqual(
            resolve_world_offset_cm((-1650.5, 162.43, 0.0), scene),
            (-1650.5, 162.43, 0.0))


if __name__ == "__main__":
    unittest.main()
