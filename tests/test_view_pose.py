"""Offline tests for the observation-camera framing rules."""

import math
import unittest

import numpy as np

from rm_sim_spear.view_pose import free_camera_pose, free_camera_state, robot_view_pose


class RobotViewPoseTest(unittest.TestCase):
    BASE = (0.0, 0.0, -140.0)

    def test_default_pitch_is_not_grazing(self) -> None:
        # The whole point of the raised default: 40-60 cm above the floor was a
        # grazing view whose ground read as haze with dashed markings.
        position, target = robot_view_pose(self.BASE)
        height = position[2] - self.BASE[2]
        horizontal = math.hypot(position[0] - self.BASE[0], position[1] - self.BASE[1])
        pitch_deg = math.degrees(math.atan2(target[2] - position[2], horizontal))
        self.assertGreater(height, 100.0)
        self.assertLess(pitch_deg, -15.0)
        self.assertGreater(pitch_deg, -60.0)

    def test_standoff_is_a_distance_not_a_per_axis_offset(self) -> None:
        for standoff in (100.0, 320.0, 900.0):
            position, _ = robot_view_pose(self.BASE, standoff_cm=standoff)
            horizontal = math.hypot(position[0] - self.BASE[0], position[1] - self.BASE[1])
            self.assertAlmostEqual(horizontal, standoff)

    def test_pitch_follows_from_standoff_and_height(self) -> None:
        # The camera always aims at the robot, so the pitch is a consequence of
        # the two knobs rather than a third one.  Assert the documented formula
        # so a future edit cannot silently tilt the default framing.
        for standoff in (150.0, 320.0, 600.0):
            for height in (60.0, 170.0):
                position, target = robot_view_pose(
                    self.BASE, standoff_cm=standoff, height_cm=height, aim_height_cm=30.0)
                expected = math.degrees(math.atan2(30.0 - height, standoff))
                actual = math.degrees(math.atan2(
                    target[2] - position[2],
                    math.hypot(position[0] - self.BASE[0], position[1] - self.BASE[1])))
                self.assertAlmostEqual(actual, expected, places=6)

    def test_azimuth_is_the_documented_diagonal(self) -> None:
        position, _ = robot_view_pose(self.BASE, standoff_cm=320.0, height_cm=170.0)
        self.assertAlmostEqual(position[0] - self.BASE[0], 320.0 / math.sqrt(2.0))
        self.assertAlmostEqual(position[1] - self.BASE[1], -320.0 / math.sqrt(2.0))
        self.assertAlmostEqual(position[2] - self.BASE[2], 170.0)

    def test_target_sits_above_the_robot_origin(self) -> None:
        _, target = robot_view_pose(self.BASE, aim_height_cm=30.0)
        self.assertAlmostEqual(target[2], self.BASE[2] + 30.0)
        self.assertAlmostEqual(target[0], self.BASE[0])
        self.assertAlmostEqual(target[1], self.BASE[1])

    def test_offsets_are_relative_to_the_posed_anchor(self) -> None:
        moved = (17.2, 29.2, -139.8)
        position, target = robot_view_pose(moved)
        reference_position, reference_target = robot_view_pose((0.0, 0.0, 0.0))
        np.testing.assert_allclose(position - np.asarray(moved), reference_position)
        np.testing.assert_allclose(target - np.asarray(moved), reference_target)

    def test_bad_base_shape_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            robot_view_pose((0.0, 0.0))


class FreeCameraTest(unittest.TestCase):
    """The bridge between a look-at pair and test_urdf_views' key handler."""

    def test_round_trip_preserves_position_and_target(self) -> None:
        for target_offset in ((100.0, 0.0, 0.0), (0.0, -200.0, 0.0), (10.0, 20.0, -170.0),
                              (-50.0, 50.0, 5.0)):
            position = np.array((96.0, -118.0, 40.0))
            target = position + np.array(target_offset)
            distance = float(np.linalg.norm(target - position))
            state = free_camera_state(position, target)
            actual_position, actual_target = free_camera_pose(state, distance)
            np.testing.assert_allclose(actual_position, position, atol=1e-9)
            np.testing.assert_allclose(actual_target, target, atol=1e-9)

    def test_angles_match_the_handlers_forward_vector(self) -> None:
        # test_urdf_views._move_debug_camera rebuilds forward as
        # [cos(p)cos(y), cos(p)sin(y), sin(p)]; if this conversion disagreed, the
        # first key press would snap the camera to a different aim.
        position = np.zeros(3)
        target = np.array((3.0, 4.0, 5.0))
        state = free_camera_state(position, target)
        yaw = math.radians(float(state["yaw"]))
        pitch = math.radians(float(state["pitch"]))
        forward = np.array([math.cos(pitch)*math.cos(yaw),
                            math.cos(pitch)*math.sin(yaw),
                            math.sin(pitch)])
        np.testing.assert_allclose(forward, target / np.linalg.norm(target), atol=1e-12)

    def test_state_does_not_alias_the_live_position(self) -> None:
        # The handler translates in place; sharing the array would move the
        # sensor pose before the caller ever reads it back.
        position = np.array((1.0, 2.0, 3.0))
        state = free_camera_state(position, position + np.array((1.0, 0.0, 0.0)))
        state["location"] += 100.0
        np.testing.assert_allclose(position, (1.0, 2.0, 3.0))

    def test_bad_shapes_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            free_camera_state((0.0, 0.0), (1.0, 2.0, 3.0))
        with self.assertRaises(ValueError):
            free_camera_state((0.0, 0.0, 0.0), (1.0, 2.0))


class FreeCameraMovementTest(unittest.TestCase):
    """What a key press does to the pair the sensor is aimed with."""

    @staticmethod
    def _translate(state, distance_cm: float) -> None:
        # Exactly what test_urdf_views._move_debug_camera does for W: translate
        # the location in place, leaving pitch/yaw alone.
        yaw = math.radians(float(state["yaw"]))
        pitch = math.radians(float(state["pitch"]))
        state["location"] += np.array([
            math.cos(pitch)*math.cos(yaw),
            math.cos(pitch)*math.sin(yaw),
            math.sin(pitch),
        ]) * distance_cm

    def test_translation_advances_along_the_unchanged_aim(self) -> None:
        position = np.array((0.0, 0.0, 0.0))
        target = np.array((10.0, 10.0, 10.0))
        distance = float(np.linalg.norm(target - position))
        state = free_camera_state(position, target)
        self._translate(state, 5.0)
        moved_position, moved_target = free_camera_pose(state, distance)
        # Aim unchanged (same direction, same standoff) and 5 cm along it.
        np.testing.assert_allclose(moved_target - moved_position, target - position)
        np.testing.assert_allclose(np.linalg.norm(moved_position - position), 5.0)

    def test_pitch_clamp_keeps_the_aim_horizontal(self) -> None:
        # _move_debug_camera clips pitch to +-89 degrees; at the clip the aim is
        # still defined, so the conversion must not divide by a zero horizontal.
        state = {"location": np.zeros(3), "pitch": 89.0, "yaw": 0.0, "roll": 0.0}
        position, target = free_camera_pose(state, 100.0)
        np.testing.assert_allclose(position, (0.0, 0.0, 0.0))
        offset = target - position
        self.assertGreater(offset[2], 99.0)
        self.assertLess(offset[2], 100.0)


if __name__ == "__main__":
    unittest.main()
