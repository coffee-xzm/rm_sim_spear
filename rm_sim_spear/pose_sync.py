"""MuJoCo-to-UE pose conversion and snapshot application.

The simulation uses right-handed meters while Unreal uses left-handed
centimeters.  This module is deliberately independent of both MuJoCo and
SPEAR so the conversion and ordering rules can be tested without an engine.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable, Mapping, Sequence

from .model import Pose, Snapshot


@dataclass(frozen=True)
class UnrealPose:
    """A world pose in Unreal units and axis convention.

    ``rotation_matrix`` is a row-major 3x3 matrix.  The SPEAR adapter below
    converts it to an ``FRotator`` through ``spear.math`` at the engine edge.
    """

    location_cm: tuple[float, float, float]
    rotation_matrix: tuple[tuple[float, float, float], ...]


def to_unreal_pose(
    pose: Pose,
    *,
    world_offset_cm: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> UnrealPose:
    """Convert a MuJoCo ``Pose`` to Unreal centimeters and orientation.

    Position uses ``100 * diag(1, -1, 1)``.  Orientation uses
    ``diag(1, -1, 1) * R * diag(1, -1, 1)`` so the result remains a proper
    rotation after the handedness change.

    ``world_offset_cm`` places the MuJoCo world origin inside the UE level: the
    robot's z=0 is its own ground contact, while ``creo_stp``'s floor sits at
    z = -160 cm, so without the offset the robot is rendered floating.
    """

    position = _finite_tuple(pose.position_m, 3, "position_m")
    quaternion = _finite_tuple(pose.quaternion_wxyz, 4, "quaternion_wxyz")
    w, x, y, z = quaternion
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if norm <= 1e-12:
        raise ValueError("quaternion_wxyz must have a non-zero norm")
    w, x, y, z = (component / norm for component in quaternion)

    rotation = (
        (
            1.0 - 2.0 * (y * y + z * z),
            2.0 * (x * y - z * w),
            2.0 * (x * z + y * w),
        ),
        (
            2.0 * (x * y + z * w),
            1.0 - 2.0 * (x * x + z * z),
            2.0 * (y * z - x * w),
        ),
        (
            2.0 * (x * z - y * w),
            2.0 * (y * z + x * w),
            1.0 - 2.0 * (x * x + y * y),
        ),
    )
    # S is diag(1, -1, 1); multiplying on both sides flips entries with
    # exactly one Y index.
    mirrored = tuple(
        tuple(rotation[row][column] * (-1.0 if (row == 1) ^ (column == 1) else 1.0)
              for column in range(3))
        for row in range(3)
    )
    return UnrealPose(
        (
            100.0 * position[0] + float(world_offset_cm[0]),
            -100.0 * position[1] + float(world_offset_cm[1]),
            100.0 * position[2] + float(world_offset_cm[2]),
        ),
        mirrored,
    )


PoseApplier = Callable[[Any, UnrealPose], None]


class PoseSynchronizer:
    """Apply committed MuJoCo poses to the configured UE link actors.

    Actors may be a subset of the MuJoCo bodies (for example, only visible
    links and the camera mount).  ``strict=True`` is useful for a full robot
    visual binding check and requires the two name sets to match exactly.
    """

    def __init__(
        self,
        actors: Mapping[str, Any],
        apply_pose: PoseApplier,
        *,
        strict: bool = False,
        world_offset_cm: tuple[float, float, float] = (0.0, 0.0, 0.0),
        order: Sequence[str] | None = None,
    ):
        if not actors:
            raise ValueError("actors must be non-empty")
        self.actors = dict(actors)
        self.apply_pose = apply_pose
        self.strict = strict
        self.world_offset_cm = tuple(float(value) for value in world_offset_cm)
        # Imported link anchors stay attached to their URDF parent while every
        # pose is applied as an absolute world location, so the application
        # *order* is observable: moving a parent after its child drags the child
        # along.  Measured in the packaged build: ``armor_link_*`` sorts before
        # ``base_link`` and ``cam_link_8`` before ``link_7``, and on the first
        # frame -- where every parent jumps from its imported level position to
        # its offset world position -- the child lands exactly one parent
        # displacement away (armor fed (15.3, 56.5, -133.0) cm, ended at
        # (15.3, 56.5, -272.8) cm, i.e. base_link's -139.8 cm again).
        # ``order`` must list parents before their children.
        self.order = tuple(order) if order else None
        self._run_id: str | None = None
        self._last_commit_id = 0

    def _application_order(self, names: set[str]) -> list[str]:
        if self.order is None:
            return sorted(names)
        rank = {name: index for index, name in enumerate(self.order)}
        return sorted(names, key=lambda name: (rank.get(name, len(rank)), name))

    def reset(self) -> None:
        self._run_id = None
        self._last_commit_id = 0

    def apply_snapshot(self, snapshot: Snapshot) -> int:
        """Apply one snapshot and return the number of actors updated."""

        commit = snapshot.commit
        if self._run_id is None:
            self._run_id = commit.run_id
        elif commit.run_id != self._run_id:
            raise ValueError("snapshot belongs to a different run; call reset()")
        if commit.commit_id <= self._last_commit_id:
            raise ValueError("snapshot commit_id must increase monotonically")

        pose_names = set(snapshot.poses)
        actor_names = set(self.actors)
        if self.strict and pose_names != actor_names:
            missing = sorted(pose_names - actor_names)
            extra = sorted(actor_names - pose_names)
            raise ValueError(f"pose/actor names do not match: missing={missing}, extra={extra}")

        updated = 0
        for name in self._application_order(actor_names & pose_names):
            self.apply_pose(
                self.actors[name],
                to_unreal_pose(snapshot.poses[name], world_offset_cm=self.world_offset_cm),
            )
            updated += 1
        self._last_commit_id = commit.commit_id
        return updated


def pose_due(iteration: int, pose_every: int) -> bool:
    """Whether this iteration should push a pose snapshot into the scene.

    The cadence is anchored at the first iteration, so ``pose_every=N`` fires on
    iterations 1, N+1, 2N+1, ... and the default ``N=1`` fires on every
    iteration.

    The previous spelling in ``tools/run_physics_sim.py``,
    ``iteration % pose_every == 1``, is true for N>=2 but is *never* true for
    N=1 (``iteration % 1`` is always 0), which silently turned the default
    configuration into "never apply a pose" and invalidated every sync-vs-async
    measurement taken while it was in place.
    """
    return (iteration - 1) % max(1, int(pose_every)) == 0


def make_spear_actor_applier(spear_math: Any, *, use_send_async: bool = True) -> PoseApplier:
    """Build an Actor callback using the installed SPEAR math helpers.

    ``spear_math`` is passed in by the SPEAR entrypoint so importing this
    module in the standalone core never imports SPEAR or NumPy.

    ``send_async`` queues the call on the engine's game thread instead of
    blocking for it, which is the right default.  ``use_send_async=False`` calls
    the actor directly, which is what the throughput measurement compares
    against: a queued call costs the frame transaction a whole extra engine
    frame on this build, while a direct call does not.
    """

    import numpy as np

    def apply(actor: Any, pose: UnrealPose) -> None:
        target = getattr(actor, "send_async", actor) if use_send_async else actor
        target.K2_SetActorLocationAndRotation(
            NewLocation=spear_math.to_spear_vector_from_numpy_array(
                numpy_array=np.asarray(pose.location_cm, dtype=float),
            ),
            NewRotation=spear_math.to_spear_rotator_from_numpy_matrix(
                numpy_matrix=np.asarray(pose.rotation_matrix, dtype=float),
            ),
            bSweep=False,
            bTeleport=True,
        )

    return apply


def _finite_tuple(value: Any, size: int, name: str) -> tuple[float, ...]:
    try:
        values = tuple(float(component) for component in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain {size} finite numbers") from exc
    if len(values) != size or not all(math.isfinite(component) for component in values):
        raise ValueError(f"{name} must contain {size} finite numbers")
    return values


Vec3 = tuple[float, float, float]


@dataclass(frozen=True)
class AttachmentCheck:
    """One link's attachment verdict from a two-pose measurement."""

    link: str
    anchor_motion_cm: float
    offset_change_cm: float
    ok: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "link": self.link,
            "anchor_motion_cm": round(self.anchor_motion_cm, 4),
            "offset_change_cm": round(self.offset_change_cm, 4),
            "ok": self.ok,
        }


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _norm(a: Vec3) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def check_attachment(
    baseline: Mapping[str, tuple[Vec3, Vec3]],
    perturbed: Mapping[str, tuple[Vec3, Vec3]],
    tolerance_cm: float = 0.5,
    min_anchor_motion_cm: float = 1.0,
) -> tuple[AttachmentCheck, ...]:
    """Verify attached actors follow their own link under a pose change.

    ``baseline`` and ``perturbed`` map a link name to ``(anchor, attached)``
    world locations measured before and after a deliberate pose change.  An
    attached actor keeps a constant offset to its anchor, so its offset change
    must stay within ``tolerance_cm`` while the anchor itself actually moved.
    That separates "attached to this link" from "happens to sit in the right
    place" and from "attached to a different link".
    """
    checks: list[AttachmentCheck] = []
    for link in sorted(set(baseline) & set(perturbed)):
        anchor_a, attached_a = baseline[link]
        anchor_b, attached_b = perturbed[link]
        anchor_motion = _norm(_sub(anchor_b, anchor_a))
        offset_change = _norm(_sub(_sub(attached_b, anchor_b), _sub(attached_a, anchor_a)))
        checks.append(AttachmentCheck(
            link=link,
            anchor_motion_cm=anchor_motion,
            offset_change_cm=offset_change,
            ok=offset_change <= tolerance_cm and anchor_motion >= min_anchor_motion_cm,
        ))
    return tuple(checks)


def unverifiable_attachment_links(
    baseline: Mapping[str, tuple[Vec3, Vec3]],
    perturbed: Mapping[str, tuple[Vec3, Vec3]],
) -> tuple[str, ...]:
    """Links present in only one of the two measurements (cannot be verified)."""
    return tuple(sorted(set(baseline) ^ set(perturbed)))
