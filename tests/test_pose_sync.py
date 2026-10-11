import unittest

from rm_sim_spear.model import FrameCommit, Pose, Snapshot
from rm_sim_spear.pose_sync import (
    PoseSynchronizer,
    check_attachment,
    make_spear_actor_applier,
    pose_due,
    to_unreal_pose,
    unverifiable_attachment_links,
)


def snapshot(run_id="run", commit_id=1, poses=None):
    return Snapshot(
        FrameCommit(run_id, commit_id, commit_id, commit_id * 1_000_000, commit_id, commit_id),
        poses or {"base": Pose((1.0, 2.0, 3.0), (1.0, 0.0, 0.0, 0.0))},
        {},
    )


class PoseSyncTests(unittest.TestCase):
    def test_spear_applier_passes_numpy_arrays_to_async_actor(self):
        calls = []

        class Actor:
            def K2_SetActorLocationAndRotation(self, **kwargs):
                calls.append(kwargs)

        class SpearMath:
            @staticmethod
            def to_spear_vector_from_numpy_array(*, numpy_array):
                return (numpy_array.shape, numpy_array.tolist())

            @staticmethod
            def to_spear_rotator_from_numpy_matrix(*, numpy_matrix):
                return (numpy_matrix.shape, numpy_matrix.tolist())

        actor = Actor()
        make_spear_actor_applier(SpearMath)(actor, to_unreal_pose(
            Pose((1.0, 2.0, 3.0), (1.0, 0.0, 0.0, 0.0)),
        ))
        self.assertEqual(calls[0]["NewLocation"][0], (3,))
        self.assertEqual(calls[0]["NewRotation"][0], (3, 3))
        self.assertTrue(calls[0]["bTeleport"])

    def test_position_and_identity_rotation_use_unreal_convention(self):
        converted = to_unreal_pose(Pose((1.0, 2.0, 3.0), (1.0, 0.0, 0.0, 0.0)))
        self.assertEqual(converted.location_cm, (100.0, -200.0, 300.0))
        self.assertEqual(converted.rotation_matrix, (
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
        ))

    def test_yaw_rotation_is_mirrored_once(self):
        converted = to_unreal_pose(Pose(
            (0.0, 0.0, 0.0),
            (2**-0.5, 0.0, 0.0, 2**-0.5),
        ))
        self.assertAlmostEqual(converted.rotation_matrix[0][1], 1.0)
        self.assertAlmostEqual(converted.rotation_matrix[1][0], -1.0)

    def test_synchronizer_applies_sorted_subset_and_rejects_stale_commits(self):
        applied = []
        sync = PoseSynchronizer(
            {"base": object(), "missing_from_snapshot": object()},
            lambda actor, pose: applied.append((actor, pose.location_cm)),
        )
        self.assertEqual(sync.apply_snapshot(snapshot()), 1)
        self.assertEqual(applied[0][1], (100.0, -200.0, 300.0))
        with self.assertRaisesRegex(ValueError, "monotonically"):
            sync.apply_snapshot(snapshot(commit_id=1))

    def test_synchronizer_requires_reset_between_runs(self):
        sync = PoseSynchronizer({"base": object()}, lambda actor, pose: None)
        sync.apply_snapshot(snapshot())
        with self.assertRaisesRegex(ValueError, "different run"):
            sync.apply_snapshot(snapshot(run_id="next", commit_id=1))
        sync.reset()
        self.assertEqual(sync.apply_snapshot(snapshot(run_id="next", commit_id=1)), 1)

    def test_invalid_quaternion_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-zero norm"):
            to_unreal_pose(Pose((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0)))


class ApplicationOrderTests(unittest.TestCase):
    """Attached anchors make the pose order observable: parents must go first."""

    def _sync(self, order):
        applied = []
        sync = PoseSynchronizer(
            {"armor_link_1": "armor", "base_link": "base", "cam_link_8": "cam"},
            lambda actor, pose: applied.append(actor),
            order=order,
        )
        sync.apply_snapshot(snapshot(poses={
            "armor_link_1": Pose((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)),
            "base_link": Pose((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)),
            "cam_link_8": Pose((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)),
        }))
        return applied

    def test_given_order_is_honoured(self):
        order = ("base_link", "armor_link_1", "cam_link_8")
        self.assertEqual(self._sync(order), ["base", "armor", "cam"])

    def test_absence_of_order_keeps_the_alphabetical_default(self):
        self.assertEqual(self._sync(None), ["armor", "base", "cam"])

    def test_names_outside_the_order_keep_a_stable_position(self):
        applied = self._sync(("base_link",))
        self.assertEqual(applied[0], "base")
        self.assertEqual(sorted(applied[1:]), ["armor", "cam"])


class LinkApplicationOrderTests(unittest.TestCase):
    """``link_application_order`` turns the plan's link tree into a pose order."""

    def test_parents_come_before_their_children(self):
        from rm_sim_spear.spear_scene_binding import link_application_order

        plan = {"links": [
            {"name": "base_link", "parent": None},
            {"name": "armor_link_1", "parent": "base_link"},
            {"name": "link_5", "parent": "base_link"},
            {"name": "cam_link_8", "parent": "link_5"},
        ]}
        order = link_application_order(plan, {"cam_link_8", "link_5", "armor_link_1", "base_link"})
        self.assertEqual(order[0], "base_link")
        self.assertLess(order.index("link_5"), order.index("cam_link_8"))
        self.assertEqual(sorted(order), ["armor_link_1", "base_link", "cam_link_8", "link_5"])

    def test_cycles_and_unknown_parents_are_tolerated(self):
        from rm_sim_spear.spear_scene_binding import link_application_order

        plan = {"links": [
            {"name": "a", "parent": "b"},
            {"name": "b", "parent": "a"},
            {"name": "c", "parent": "not_bound"},
        ]}
        # A cycle has no valid order; what matters is that it terminates and
        # emits every name exactly once.
        self.assertEqual(sorted(link_application_order(plan, {"a", "b", "c"})),
                         ["a", "b", "c"])
        self.assertEqual(link_application_order({"links": []}, {"b", "a"}), ["a", "b"])



class AttachmentCheckTests(unittest.TestCase):
    """The two-pose test that proves an armor clone follows its own link."""

    def test_following_clone_passes(self):
        # anchor moved 30 cm in Z; the attached actor moved with it
        baseline = {"armor_link_1": ((0.0, 0.0, 10.0), (5.0, 0.0, 12.0))}
        perturbed = {"armor_link_1": ((0.0, 0.0, 40.0), (5.0, 0.0, 42.0))}
        checks = check_attachment(baseline, perturbed)
        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0].ok)
        self.assertAlmostEqual(checks[0].anchor_motion_cm, 30.0)
        self.assertAlmostEqual(checks[0].offset_change_cm, 0.0)

    def test_unattached_clone_is_caught(self):
        # the visual stayed behind while its anchor moved
        baseline = {"armor_link_2": ((0.0, 0.0, 10.0), (5.0, 0.0, 12.0))}
        perturbed = {"armor_link_2": ((0.0, 0.0, 40.0), (5.0, 0.0, 12.0))}
        checks = check_attachment(baseline, perturbed)
        self.assertFalse(checks[0].ok)
        self.assertAlmostEqual(checks[0].offset_change_cm, 30.0)

    def test_clone_attached_to_the_wrong_link_is_caught(self):
        # armor_link_1's actor actually followed armor_link_2
        baseline = {"armor_link_1": ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0))}
        perturbed = {"armor_link_1": ((0.0, 0.0, 10.0), (10.0, 0.0, 25.0))}
        checks = check_attachment(baseline, perturbed)
        self.assertFalse(checks[0].ok)

    def test_a_stationary_anchor_cannot_verify_anything(self):
        baseline = {"base_link": ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))}
        perturbed = {"base_link": ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))}
        checks = check_attachment(baseline, perturbed)
        self.assertFalse(checks[0].ok)
        self.assertAlmostEqual(checks[0].anchor_motion_cm, 0.0)

    def test_links_seen_in_only_one_measurement_are_reported(self):
        baseline = {"a": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))}
        perturbed = {"b": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))}
        self.assertEqual(unverifiable_attachment_links(baseline, perturbed), ("a", "b"))
        self.assertEqual(check_attachment(baseline, perturbed), ())


class WorldOffsetTests(unittest.TestCase):
    """MuJoCo z=0 is the robot's own ground contact, not the level's floor."""

    def _pose(self):
        return Pose(position_m=(0.1, 0.2, 0.05), quaternion_wxyz=(1.0, 0.0, 0.0, 0.0))

    def test_default_offset_keeps_the_documented_conversion(self) -> None:
        base = to_unreal_pose(self._pose())
        self.assertEqual(base.location_cm, (10.0, -20.0, 5.0))

    def test_offset_only_translates(self) -> None:
        shifted = to_unreal_pose(self._pose(), world_offset_cm=(0.0, 0.0, -160.0))
        self.assertEqual(shifted.location_cm, (10.0, -20.0, -155.0))
        self.assertEqual(shifted.rotation_matrix, to_unreal_pose(self._pose()).rotation_matrix)


class PoseDueTest(unittest.TestCase):
    """`--pose-every N` must fire on 1, N+1, 2N+1 and every frame for N=1.

    The shipped bug spelled the guard `iteration % N == 1`, which is never true
    for the default N=1: every measurement taken with that default was measuring
    a transaction with no pose calls in it.
    """

    def test_default_applies_every_frame(self):
        self.assertEqual([i for i in range(1, 7) if pose_due(i, 1)], [1, 2, 3, 4, 5, 6])

    def test_cadence_is_anchored_at_the_first_iteration(self):
        self.assertEqual([i for i in range(1, 13) if pose_due(i, 2)], [1, 3, 5, 7, 9, 11])
        self.assertEqual([i for i in range(1, 13) if pose_due(i, 3)], [1, 4, 7, 10])
        self.assertEqual([i for i in range(1, 13) if pose_due(i, 5)], [1, 6, 11])

    def test_non_positive_pose_every_is_treated_as_one(self):
        self.assertEqual([i for i in range(1, 5) if pose_due(i, 0)], [1, 2, 3, 4])
