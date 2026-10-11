"""The generated URDF describes a robot, not a world.

These tests pin the structural contract of ``PhysicsOptions``: without options the
driver keeps loading the generated model unchanged (``base_link`` welded to
``world``, no actuators), and with options the model gains a free base, a ground
plane and one velocity actuator per driven joint.  Dynamic stability of the
simplified collision model is tracked separately; here we only require finite
numbers after ``mj_forward`` and one step.
"""

import unittest
from pathlib import Path

import mujoco
import numpy as np

from rm_sim_spear.mujoco_driver import (
    COLLISION_GEOM_GROUP,
    MujocoDriver,
    PhysicsOptions,
    wheel_cylinder,
)

MODEL = Path(__file__).resolve().parents[1] / "generated" / "urdf" / "总装" / "mujoco.urdf"
WHEELS = ("link_1_joint", "link_2_joint", "link_3_joint", "link_4_joint")


def _wheel_surface(model, data, joint_name: str):
    """``(radial, axial, lowest_world_z)`` over one wheel's mesh vertices.

    Vertices are moved into the body frame by each geom's own ``pos`` and
    ``quat`` -- the step a union of ``geom_aabb`` cannot do -- and then into the
    world by the body's *settled* pose, so ``lowest_world_z`` is how far the CAD
    reaches below the plane at ``z = 0``.
    """
    joint = model.joint(joint_name)
    body_id = int(joint.bodyid[0])
    axis = np.asarray(joint.axis, dtype=float)
    axis = axis / np.linalg.norm(axis)
    anchor = np.asarray(joint.pos, dtype=float)
    radial, axial, world = [], [], []
    for index in range(model.ngeom):
        if (int(model.geom_bodyid[index]) != body_id
                or int(model.geom_type[index]) != int(mujoco.mjtGeom.mjGEOM_MESH)):
            continue
        geom = model.geom(index)
        matrix = np.zeros(9)
        mujoco.mju_quat2Mat(matrix, np.asarray(geom.quat, dtype=float))
        data_id = int(model.geom_dataid[index])
        start = int(model.mesh_vertadr[data_id])
        stop = start + int(model.mesh_vertnum[data_id])
        local = (np.asarray(model.mesh_vert[start:stop], dtype=float)
                 @ matrix.reshape(3, 3).T + np.asarray(geom.pos, dtype=float))
        relative = local - anchor
        axial.append(relative @ axis)
        radial.append(np.linalg.norm(relative - axial[-1][:, None] * axis, axis=1))
        world.append(local @ data.xmat[body_id].reshape(3, 3).T + data.xpos[body_id])
    return (np.concatenate(radial), np.concatenate(axial),
            float(np.concatenate(world)[:, 2].min()))


@unittest.skipUnless(MODEL.is_file(), f"generated model missing: {MODEL}")
class PhysicsOptionsTests(unittest.TestCase):
    def test_default_keeps_the_generated_model_untouched(self) -> None:
        driver = MujocoDriver.from_xml_path(MODEL, steps_per_frame=1)
        # 4 wheel hinges + gimbal yaw/pitch; base_link is welded to world.
        self.assertEqual(driver.model.nq, 6)
        self.assertEqual(driver.model.nv, 6)
        self.assertEqual(driver.model.nu, 0)
        base = driver.model.body("base_link")
        self.assertEqual(str(driver.model.body(int(base.parentid[0])).name), "world")

    def test_free_base_floor_and_drive_actuators(self) -> None:
        options = PhysicsOptions(free_base=True, driven_joints=WHEELS, floor=True)
        driver = MujocoDriver.from_xml_path(MODEL, steps_per_frame=1, physics=options)
        model = driver.model
        # free joint adds 7 qpos / 6 dof on top of the 6 hinges.
        self.assertEqual(model.nq, 13)
        self.assertEqual(model.nv, 12)
        self.assertEqual(model.nu, len(WHEELS))
        self.assertEqual(
            [str(model.actuator(i).name) for i in range(model.nu)],
            [f"drive_{name}" for name in WHEELS],
        )
        joint = model.joint(WHEELS[0])
        self.assertEqual(int(joint.type[0]), int(mujoco.mjtJoint.mjJNT_HINGE))
        self.assertAlmostEqual(float(model.dof_armature[joint.dofadr[0]]), 0.01)
        # The free joint lives on the root body.
        base = model.body("base_link")
        free = [model.joint(i) for i in range(model.njnt)
                if int(model.joint(i).bodyid[0]) == base.id
                and int(model.joint(i).type[0]) == int(mujoco.mjtJoint.mjJNT_FREE)]
        self.assertEqual(len(free), 1)
        # ... and the world owns a ground plane.
        floor = [model.geom(i) for i in range(model.ngeom)
                 if int(model.geom(i).bodyid[0]) == 0]
        self.assertEqual([str(g.name) for g in floor], ["floor"])

    def test_free_base_without_floor_is_allowed_but_would_fall_forever(self) -> None:
        options = PhysicsOptions(free_base=True, driven_joints=WHEELS, floor=False)
        driver = MujocoDriver.from_xml_path(MODEL, steps_per_frame=1, physics=options)
        world_geoms = [i for i in range(driver.model.ngeom)
                       if int(driver.model.geom(i).bodyid[0]) == 0]
        self.assertEqual(world_geoms, [])

    def test_step_is_finite(self) -> None:
        options = PhysicsOptions(free_base=True, driven_joints=WHEELS, floor=True)
        driver = MujocoDriver.from_xml_path(MODEL, steps_per_frame=1, physics=options)
        driver.step((0.0,) * driver.model.nu)
        self.assertTrue(all(value == value for value in driver.data.qpos))  # NaN != NaN
        self.assertTrue(all(value == value for value in driver.data.qvel))

    def test_control_length_is_validated(self) -> None:
        options = PhysicsOptions(free_base=True, driven_joints=WHEELS, floor=True)
        driver = MujocoDriver.from_xml_path(MODEL, steps_per_frame=1, physics=options)
        with self.assertRaises(ValueError):
            driver.step((0.0,))

    def test_unknown_root_link_and_joint_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PhysicsOptions(free_base=True, root_link="does_not_exist").build_spec(MODEL)
        with self.assertRaises(ValueError):
            PhysicsOptions(driven_joints=("does_not_exist",)).build_spec(MODEL)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(MODEL.is_file(), f"generated model missing: {MODEL}")
class GroundedDriveTests(unittest.TestCase):
    """Acceptance for "the robot advances under physics".

    The numbers are deliberately loose bands: this pins the *behaviour* (stands on
    its wheels, does not diverge, translates when the wheels are commanded) rather
    than one particular contact tuning.
    """

    def _driver(self):
        options = PhysicsOptions(
            free_base=True,
            driven_joints=WHEELS,
            floor=True,
            joint_damping=2.0,
            simple_collision=True,
            timestep=0.001,
            drive_gain=1.0,
            joint_armature=0.01,
        )
        return MujocoDriver.from_xml_path(MODEL, steps_per_frame=5, physics=options)

    def test_simple_collision_keeps_only_floor_and_wheels(self) -> None:
        driver = self._driver()
        colliding = sorted(
            str(driver.model.geom(index).name)
            for index in range(driver.model.ngeom)
            if int(driver.model.geom(index).contype[0]) or int(driver.model.geom(index).conaffinity[0])
        )
        self.assertEqual(colliding, sorted(["floor"] + [f"wheel_{name}" for name in WHEELS]))

    def test_shell_bodies_add_one_box_each_on_top_of_the_wheels(self) -> None:
        """A shell body gets exactly one box, and the wheels stay cylinders.

        The shell exists so the chassis cannot drive through a wall.  Which bodies
        are safe is a *measured* choice, not a derived one: ``link_5``'s AABB
        reaches below the wheels and puts the robot on its gimbal.
        """
        options = PhysicsOptions(
            free_base=True, driven_joints=WHEELS, floor=True, joint_damping=2.0,
            simple_collision=True, shell_bodies=("base_link",), timestep=0.001,
            drive_gain=1.0, joint_armature=0.01,
        )
        driver = MujocoDriver.from_xml_path(MODEL, steps_per_frame=5, physics=options)
        model = driver.model
        colliding = sorted(
            str(model.geom(index).name)
            for index in range(model.ngeom)
            if int(model.geom(index).contype[0]) or int(model.geom(index).conaffinity[0])
        )
        self.assertEqual(colliding, sorted(
            ["floor", "shell_base_link"] + [f"wheel_{name}" for name in WHEELS]))
        shell = model.geom(mujoco.mj_name2id(
            model, mujoco.mjtObj.mjOBJ_GEOM, "shell_base_link"))
        self.assertEqual(int(shell.type[0]), int(mujoco.mjtGeom.mjGEOM_BOX))
        # Sized from the base_link mesh set, so it must be non-degenerate.
        self.assertTrue(all(float(shell.size[i]) > 0.01 for i in range(3)))

    def test_collider_geometry_is_not_in_the_default_picture(self) -> None:
        """Every collider but the floor lives in the hidden group 3.

        MuJoCo draws groups 0/1/2 by default, and the URDF importer puts
        ``<collision>`` in group 0 and ``<visual>`` in group 1 -- measured on
        总装: 955 mesh geoms in each.  Left alone, the exported collision shell
        is painted over the robot and the mechanism is invisible in
        ``mujoco.viewer``, which is the only place the collision model can be
        inspected at all.  The floor is exempt on purpose: it is the ground
        reference, not a hull.
        """
        options = PhysicsOptions(
            free_base=True, driven_joints=WHEELS, floor=True, joint_damping=2.0,
            simple_collision=True, shell_bodies=("base_link",), timestep=0.001,
        )
        driver = MujocoDriver.from_xml_path(MODEL, steps_per_frame=5, physics=options)
        model = driver.model
        visible_colliders = sorted(
            str(model.geom(index).name)
            for index in range(model.ngeom)
            if (int(model.geom(index).contype[0]) or int(model.geom(index).conaffinity[0]))
            and int(model.geom(index).group[0]) != COLLISION_GEOM_GROUP
        )
        self.assertEqual(visible_colliders, ["floor"])
        # The muted export colliders have to be hidden as well, otherwise they
        # stay painted over the visuals even though they no longer touch
        # anything: that was the actual complaint.
        visible_mesh = [index for index in range(model.ngeom)
                        if int(model.geom(index).group[0]) != COLLISION_GEOM_GROUP
                        and int(model.geom(index).type[0]) == int(mujoco.mjtGeom.mjGEOM_MESH)]
        hidden_mesh = [index for index in range(model.ngeom)
                       if int(model.geom(index).group[0]) == COLLISION_GEOM_GROUP
                       and int(model.geom(index).type[0]) == int(mujoco.mjtGeom.mjGEOM_MESH)]
        # 955 visuals kept, 955 export colliders moved out of the way.
        self.assertEqual(len(visible_mesh), 955)
        self.assertEqual(len(hidden_mesh), 955)
        # Drawing groups must not have cost a single contact.
        for _ in range(600):
            driver.step((0.0,) * model.nu)
        base = driver.read_poses()["base_link"].position_m[2]
        self.assertAlmostEqual(base * 1000.0, 73.478, delta=1.0)

    def test_urdf_colliders_are_hidden_without_simple_collision_too(self) -> None:
        """The plain path keeps the export's colliders live -- but not drawn.

        ``simple_collision=False`` is still the fallback, and there the export's
        own ``<collision>`` meshes *are* the collision model.  Hiding them is a
        display decision, so it must not depend on which path built the model.
        """
        options = PhysicsOptions(free_base=True, driven_joints=WHEELS, floor=True)
        model = MujocoDriver.from_xml_path(MODEL, steps_per_frame=5,
                                           physics=options).model
        live = [index for index in range(model.ngeom)
                if int(model.geom(index).contype[0]) or int(model.geom(index).conaffinity[0])]
        self.assertGreater(len(live), 900)
        drawn_live = [str(model.geom(index).name) for index in live
                      if int(model.geom(index).group[0]) != COLLISION_GEOM_GROUP]
        self.assertEqual(drawn_live, ["floor"])

    def test_a_shell_naming_no_mesh_raises_instead_of_silently_dropping(self) -> None:
        """A typo must be loud: a dropped shell body is an invisible regression."""
        options = PhysicsOptions(
            free_base=True, driven_joints=WHEELS, floor=True,
            simple_collision=True, shell_bodies=("no_such_body",), timestep=0.001,
        )
        with self.assertRaises(Exception):
            MujocoDriver.from_xml_path(MODEL, steps_per_frame=5, physics=options)

    def test_gimbal_has_no_collider_and_stays_inside_its_urdf_limits(self) -> None:
        """The gimbal is constrained by its ``<limit>``, not by geometry.

        Owner decision 2026-10-10: the chassis gets a collider, the gimbal does
        not -- it just has to respect the URDF ranges.  MuJoCo already marks both
        gimbal hinges ``limited``; this pins that the settled pose never leaves
        the range, so a future export can't silently drop the limits.
        """
        driver = self._driver()
        model = driver.model
        colliding = {str(model.geom(index).name) for index in range(model.ngeom)
                     if int(model.geom(index).contype[0]) or int(model.geom(index).conaffinity[0])}
        self.assertFalse({name for name in colliding if "link_5" in name or "link_7" in name})

        gimbal = {"link_5_joint": (-0.30, 0.75), "link_7_joint": (-1.40, 0.30)}
        for name in gimbal:
            jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
            self.assertEqual(int(model.jnt_limited[jid]), 1, f"{name} lost its limit")

        data = driver.data
        for _ in range(4000):
            data.ctrl[:] = 0.0
            mujoco.mj_step(model, data)
        for name, (lower, upper) in gimbal.items():
            jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
            angle = float(data.qpos[model.jnt_qposadr[jid]])
            # 1e-2 rad (0.57 deg): gravity holds these against a limit and
            # MuJoCo's limit is a *soft* constraint, so the settled angle sits a
            # couple of milliradians outside it -- measured 2.3e-3 rad on
            # link_5_joint.  A joint that had lost its limit would swing by
            # ~1 rad, so this still catches the regression by two orders.
            self.assertGreater(angle, lower - 1e-2, f"{name} fell below its limit")
            self.assertLess(angle, upper + 1e-2, f"{name} ran past its limit")

    def test_wheel_cylinder_is_the_rolling_surface(self) -> None:
        """The cylinder has to be sized from the tyre, not from one part's AABB.

        ``model.geom_aabb`` is expressed **in each geom's own frame**, so a union
        of those ignores every part's ``pos``/``quat`` and collapses to "largest
        single part" -- for these omni wheels the 55.5 mm side plate, 4.6 mm
        inside the 60.1 mm rollers, which sank the CAD into the floor.
        """
        driver = self._driver()
        model = driver.model
        radial, axial, _ = _wheel_surface(model, driver.data, WHEELS[0])
        radius, half_length, centre = wheel_cylinder(model, WHEELS[0])
        self.assertAlmostEqual(radius, float(radial.max()), places=9)
        self.assertGreater(radius, 0.058)
        # ... and the emitted collider carries those numbers.
        geom = model.geom(mujoco.mj_name2id(
            model, mujoco.mjtObj.mjOBJ_GEOM, f"wheel_{WHEELS[0]}"))
        self.assertAlmostEqual(float(geom.size[0]), radius, places=9)
        self.assertAlmostEqual(float(geom.size[1]), half_length, places=9)
        self.assertAlmostEqual(float(geom.pos[2]), centre, places=9)
        # The band that reaches the rim is the tyre, not the whole hub.
        self.assertGreater(half_length, 0.010)
        self.assertLess(half_length, 0.030)
        self.assertLess(float(axial.min()), centre)
        self.assertGreater(float(axial.max()), centre)

    def test_wheels_touch_the_plane_instead_of_sinking_through_it(self) -> None:
        """Acceptance for "the robot stands on the level's floor".

        The plane at ``z = 0`` is what ``--world-offset-cm`` lines the level's
        floor up with, so the CAD has to reach it and stop there.  Before the
        rolling-surface fix the four wheels ended up 0.4/3.0/6.4/9.3 mm under it.
        """
        driver = self._driver()
        for _ in range(3000):
            driver.data.ctrl[:] = 0.0
            mujoco.mj_step(driver.model, driver.data)
        self.assertLess(float(abs(driver.data.qvel).max()), 1.0)
        for name in WHEELS:
            _, _, lowest = _wheel_surface(driver.model, driver.data, name)
            self.assertGreater(lowest, -0.001)  # contact penetration only
            self.assertLess(lowest, 0.001)      # ... and the tyre really is down

    def test_robot_settles_on_its_wheels_without_diverging(self) -> None:
        driver = self._driver()
        driver.data.qpos[2] = 0.12
        mujoco.mj_forward(driver.model, driver.data)
        for _ in range(80):
            driver.step((0.0,) * driver.model.nu)
        z = driver.read_poses()["base_link"].position_m[2]
        self.assertGreater(z, 0.03)
        self.assertLess(z, 0.10)
        self.assertLess(float(abs(driver.data.qvel).max()), 20.0)
        self.assertFalse(any(value != value for value in driver.data.qpos))

    def test_wheel_drive_stays_finite_and_monotonic(self) -> None:
        """A stiff velocity servo (gain 10) overflows QACC and the trace goes
        meaningless; the shipped defaults must keep time monotonic and the
        chassis on its wheels."""
        driver = self._driver()
        driver.data.qpos[2] = 0.12
        mujoco.mj_forward(driver.model, driver.data)
        times = []
        for _ in range(20):
            driver.step((0.0,) * driver.model.nu)
            times.append(driver.data.time)
        settled_z = driver.read_poses()["base_link"].position_m[2]
        for _ in range(20):
            driver.step((6.0,) * driver.model.nu)
            times.append(driver.data.time)
        self.assertTrue(all(b >= a for a, b in zip(times, times[1:])))
        self.assertLess(float(abs(driver.data.qvel).max()), 100.0)
        self.assertLess(abs(driver.read_poses()["base_link"].position_m[2] - settled_z), 0.05)
        self.assertGreater(settled_z, 0.03)

    def test_wheel_command_moves_the_chassis(self) -> None:
        driver = self._driver()
        driver.data.qpos[2] = 0.12
        mujoco.mj_forward(driver.model, driver.data)
        for _ in range(80):
            driver.step((0.0,) * driver.model.nu)
        start = driver.read_poses()["base_link"].position_m
        for _ in range(160):
            driver.step((6.0,) * driver.model.nu)
        end = driver.read_poses()["base_link"].position_m
        moved = sum((end[axis] - start[axis]) ** 2 for axis in range(3)) ** 0.5
        self.assertGreater(moved, 0.03)
        self.assertFalse(any(value != value for value in driver.data.qpos))


class ViewerDefaultsTest(unittest.TestCase):
    """The interactive viewer is opt-in and must stay inert until opened.

    ``--mujoco-viewer`` mirrors SPEAR's examples/mujoco_interop pattern
    (``launch_passive`` + one ``sync()`` per iteration).  Headless runs -- and
    the offline suite -- must never import the GUI module or open a window, so
    the accessors have to answer sanely with no viewer attached.
    """

    def test_no_viewer_by_default(self):
        model = mujoco.MjModel.from_xml_string(
            "<mujoco><worldbody><body name='b'><geom type='sphere' size='0.1'/>"
            "</body></worldbody></mujoco>")
        driver = MujocoDriver(model, steps_per_frame=1)
        self.assertFalse(driver.viewer_running())
        self.assertFalse(driver.sync_viewer())
        driver.close_viewer()  # must not raise
