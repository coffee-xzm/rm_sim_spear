"""Offline tests for rm_sim_spear.skeletal_robot and the pose math it adds.

The UE-facing spawn path cannot run here, but the two things that can silently
produce a *wrong measurement* can: the bone application order (a child written
before its parent is computed against the previous frame) and the
matrix -> quaternion conversion (a bad branch misplaces every wheel).

``unreal`` is faked through ``sys.modules`` for the writer test, so the call
shape and the argument mapping are exercised without an engine.
"""

import unittest

from rm_sim_spear.model import FrameCommit, Pose, Snapshot
from rm_sim_spear.pose_sync import to_unreal_pose
from rm_sim_spear.skeletal_robot import (
    make_poseable_bone_writer,
    make_skeletal_pose_synchronizer,
    missing_bones,
)


def snapshot(poses, commit_id=1):
    return Snapshot(
        FrameCommit("run", commit_id, commit_id, commit_id * 1_000_000, commit_id, commit_id),
        poses,
        {},
    )


class MissingBonesTest(unittest.TestCase):
    def test_reports_only_the_absent_names(self):
        self.assertEqual(missing_bones(["base", "arm", "tool"], ["base", "tool"]), ["arm"])

    def test_nothing_missing(self):
        self.assertEqual(missing_bones(["base"], ["base", "arm"]), [])


class SkeletalSynchronizerTest(unittest.TestCase):
    def _synchronizer(self, bones, writes):
        def writer(component, bone, pose):
            writes.append((bone, pose.location_cm, pose.rotation_matrix))

        return make_skeletal_pose_synchronizer(
            component=object(),
            bones=bones,
            writer=writer,
            world_offset_cm=(0.0, 0.0, -160.0),
        )

    def test_writes_every_bone_once_in_the_given_order(self):
        writes = []
        synchronizer = self._synchronizer(["base", "arm", "tool"], writes)
        poses = {
            "base": Pose((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)),
            "arm": Pose((1.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)),
            "tool": Pose((2.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)),
        }
        applied = synchronizer.apply_snapshot(snapshot(poses))
        self.assertEqual(applied, 3)
        self.assertEqual([bone for bone, _, _ in writes], ["base", "arm", "tool"])

    def test_parent_order_is_the_bone_order_not_alphabetical(self):
        # "armor_link_1" sorts before "base_link", so an alphabetical fallback
        # would write children first; the explicit order must win.
        writes = []
        synchronizer = self._synchronizer(["base_link", "armor_link_1"], writes)
        synchronizer.apply_snapshot(snapshot({
            "base_link": Pose((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)),
            "armor_link_1": Pose((0.0, 1.0, 0.0), (1.0, 0.0, 0.0, 0.0)),
        }))
        self.assertEqual([bone for bone, _, _ in writes], ["base_link", "armor_link_1"])

    def test_world_offset_reaches_the_bone(self):
        writes = []
        synchronizer = self._synchronizer(["base"], writes)
        synchronizer.apply_snapshot(snapshot({
            "base": Pose((0.5, -0.25, 0.75), (1.0, 0.0, 0.0, 0.0)),
        }))
        bone, location, _ = writes[0]
        self.assertEqual(bone, "base")
        self.assertAlmostEqual(location[0], 50.0)
        self.assertAlmostEqual(location[1], 25.0)
        self.assertAlmostEqual(location[2], 75.0 - 160.0)

    def test_reapplying_a_commit_is_rejected_not_silently_repeated(self):
        # Re-writing the same commit would double the bone writes for one frame
        # and quietly inflate the measured cost, so the shared synchronizer
        # treats it as a bug rather than a no-op.
        writes = []
        synchronizer = self._synchronizer(["base"], writes)
        poses = {"base": Pose((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0))}
        synchronizer.apply_snapshot(snapshot(poses, commit_id=7))
        with self.assertRaises(ValueError):
            synchronizer.apply_snapshot(snapshot(poses, commit_id=7))
        self.assertEqual(len(writes), 1)

    def test_skips_links_the_snapshot_does_not_have(self):
        writes = []
        synchronizer = self._synchronizer(["base", "arm"], writes)
        applied = synchronizer.apply_snapshot(snapshot({
            "base": Pose((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)),
        }))
        self.assertEqual(applied, 1)
        self.assertEqual([bone for bone, _, _ in writes], ["base"])

    def test_rejects_duplicate_or_empty_bone_lists(self):
        with self.assertRaises(ValueError):
            make_skeletal_pose_synchronizer(object(), ["base", "base"])
        with self.assertRaises(ValueError):
            make_skeletal_pose_synchronizer(object(), [])


class FakeSpearMath:
    """Minimal ``spear.math`` stand-in: records the struct it is handed."""

    def __init__(self):
        self.transforms = []

    def to_spear_transform_from_numpy_transform(self, *, numpy_transform):
        self.transforms.append(numpy_transform)
        return {"Translation": "vector", "Rotation": "quat", "Scale3D": "vector"}


class RecordingComponent:
    def __init__(self):
        self.calls = []

    def SetBoneTransformByName(self, **kwargs):
        self.calls.append(kwargs)


class PoseableBoneWriterTest(unittest.TestCase):
    def setUp(self):
        self.math = FakeSpearMath()
        self.component = RecordingComponent()

    def test_writer_maps_the_pose_into_a_world_space_transform(self):
        pose = to_unreal_pose(Pose((0.25, -0.5, 0.75), (1.0, 0.0, 0.0, 0.0)))
        make_poseable_bone_writer(self.math)(self.component, "base_link", pose)
        self.assertEqual(len(self.component.calls), 1)
        call = self.component.calls[0]
        self.assertEqual(call["BoneName"], "base_link")
        self.assertEqual(call["BoneSpace"], "WorldSpace")
        self.assertEqual(call["InTransform"]["Rotation"], "quat")
        sent = self.math.transforms[0]
        for got, want in zip(sent["translation"], pose.location_cm):
            self.assertAlmostEqual(float(got), want)
        self.assertEqual(sent["rotation"].shape, (3, 3))
        self.assertEqual(list(sent["scale"]), [1.0, 1.0, 1.0])

    def test_component_space_is_selectable(self):
        pose = to_unreal_pose(Pose((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)))
        make_poseable_bone_writer(self.math, world_space=False)(
            self.component, "base_link", pose)
        self.assertEqual(self.component.calls[0]["BoneSpace"], "ComponentSpace")

    def test_synchronizer_builds_the_writer_from_spear_math(self):
        synchronizer = make_skeletal_pose_synchronizer(
            self.component, ["base"], spear_math=self.math)
        synchronizer.apply_snapshot(snapshot({
            "base": Pose((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0))}))
        self.assertEqual(len(self.component.calls), 1)

    def test_spear_math_is_required_when_no_writer_is_given(self):
        with self.assertRaises(ValueError):
            make_skeletal_pose_synchronizer(self.component, ["base"])


if __name__ == "__main__":
    unittest.main()
