"""Drive a skinned robot: one ``UPoseableMeshComponent``, one bone per URDF link.

Why this exists
---------------
Measured on the ``总装`` robot, the per-frame pose cost is dominated by UE
re-running ``View Visibility`` / ``InitViews`` whenever a scene component's
transform changes, and it scales with the number of components that move (see
``docs/render_budget.md`` §18).  Posing 13 link anchors costs ~9.5 ms of game
thread and ~12 ms of render thread; even instancing the 967 visual actors down
to 244 components did not help.

A skeletal mesh sidesteps that machinery: the component's transform never
changes, only its bones do.  The hypothesis under test is that a frame which
writes 13 bones costs nothing in ``FScene::UpdatePrimitiveTransform``.

How the bones are written
-------------------------
``UPoseableMeshComponent`` is the engine's "pose bones from code" component --
``USkeletalMeshComponent`` has no bone *setters* at all.  Its
``SetBoneTransformByName`` takes an ``EBoneSpaces::Type``, so a MuJoCo world
pose can be written directly as ``WorldSpace``, with no frame math beyond the
existing :func:`rm_sim_spear.pose_sync.to_unreal_pose`.

One trap, and it is the same trap the actor path has: ``SetBoneTransformByName``
converts world space down to bone space using the *parent bone's current*
component-space transform, then marks the pose dirty.  A child written before
its parent is therefore computed against the previous frame's parent, so the
application order must list parents first.  That is exactly what
:func:`rm_sim_spear.spear_scene_binding.link_application_order` produces.

Talking to the engine
---------------------
This module runs in the **client** process, which has no ``unreal`` module: the
objects handed back by ``as_unreal_object=True`` are ``spear.UnrealObject``
proxies that forward any ``UFUNCTION`` as a game-thread RPC.  Two consequences
shape the code below, and both have a precedent in this repository:

* every argument is passed **by keyword** -- the proxy's generated caller
  accepts ``*args`` and silently drops them;
* structs are plain SPEAR dictionaries (``{"Translation", "Rotation",
  "Scale3D"}``) and enums are the enumerator *name* as a string, as in
  ``SetMobility(NewMobility="Movable")``.

Layering
--------
:class:`PoseSynchronizer` already owns the commit-id bookkeeping, the world
offset and the parent-first ordering.  Rather than write a second copy of that
logic, each bone is wrapped in a tiny :class:`_BoneTarget` adapter so the tested
actor synchronizer can drive bones unchanged.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .pose_sync import PoseSynchronizer, UnrealPose

#: Native class of the component that can pose bones; resolved by path because
#: SPEAR's static class-descriptor registry does not contain every engine class.
POSEABLE_COMPONENT_CLASS = "/Script/Engine.PoseableMeshComponent"

#: ``EBoneSpaces`` has only these two enumerators (``LocalSpace`` is commented
#: out in the engine header), and SPEAR marshals an enum from its name.
_BONE_SPACE_NAMES = {True: "WorldSpace", False: "ComponentSpace"}


class _BoneTarget:
    """Make one bone of a poseable component look like a poseable actor.

    ``PoseSynchronizer`` calls ``apply_pose(target, pose)`` and never passes the
    link name, so the name has to live on the target.  Keeping this an adapter
    means the ordering, commit-id and world-offset behaviour is the already
    tested one rather than a parallel implementation.
    """

    __slots__ = ("component", "bone", "_writer")

    def __init__(self, component: Any, bone: str, writer: Any):
        self.component = component
        self.bone = bone
        self._writer = writer

    def apply(self, pose: UnrealPose) -> None:
        self._writer(self.component, self.bone, pose)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"_BoneTarget({self.bone!r})"


def make_poseable_bone_writer(spear_math: Any, *, world_space: bool = True):
    """Return ``writer(component, bone_name, pose)`` for a poseable component.

    ``spear_math`` is passed in by the entrypoint, exactly as
    :func:`rm_sim_spear.pose_sync.make_spear_actor_applier` takes it, so this
    module imports neither SPEAR nor NumPy at module scope.
    """
    space = _BONE_SPACE_NAMES[bool(world_space)]

    def write(component: Any, bone: str, pose: UnrealPose) -> None:
        import numpy as np

        # ``to_spear_transform_from_numpy_transform`` is the validated path:
        # ``convert_transform(as_spear=True)`` is what produces the canonical
        # {"Translation", "Rotation", "Scale3D"} dict, with the rotation as a
        # quaternion.  (``to_spear_transform_from_numpy_matrix`` builds a
        # *rotator* dict instead, which is not what an FTransform field takes.)
        transform = spear_math.to_spear_transform_from_numpy_transform(
            numpy_transform={
                "translation": np.asarray(pose.location_cm, dtype=float),
                "rotation": np.asarray(pose.rotation_matrix, dtype=float),
                "scale": np.ones(3),
            }
        )
        component.SetBoneTransformByName(
            BoneName=bone,
            InTransform=transform,
            BoneSpace=space,
        )

    return write


def make_skeletal_pose_synchronizer(
    component: Any,
    bones: Sequence[str],
    *,
    spear_math: Any = None,
    writer: Any = None,
    strict: bool = False,
    world_offset_cm: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> PoseSynchronizer:
    """Bind a poseable component's bones to a parent-first list of link names.

    ``bones`` must already be in parent-before-child order; callers get that
    from :func:`link_application_order`.  The order is passed straight through
    to :class:`PoseSynchronizer` so its shuffle guard applies here too.

    ``writer`` overrides the engine-facing callback; that is the seam the
    offline tests use, so ``spear_math`` is only required when it is omitted.
    """
    names = [str(name) for name in bones]
    if not names:
        raise ValueError("bones must be non-empty")
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate bone names in {names!r}")
    if writer is None:
        if spear_math is None:
            raise ValueError("spear_math is required when no writer is supplied")
        writer = make_poseable_bone_writer(spear_math)

    def apply_pose(target: _BoneTarget, pose: UnrealPose) -> None:
        target.apply(pose)

    targets = {name: _BoneTarget(component, name, writer) for name in names}
    return PoseSynchronizer(
        actors=targets,
        apply_pose=apply_pose,
        strict=strict,
        world_offset_cm=world_offset_cm,
        order=names,
    )


def spawn_poseable_robot(
    game: Any,
    *,
    asset_path: str,
    stable_name: str | None = None,
    component_name: str = "RMSimSkeletalMesh",
    cast_shadow: bool = True,
    diagnostics: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    """Spawn a movable actor whose root is one poseable component.

    Must be called inside a SPEAR frame transaction.  The component itself is
    made the actor's root: there is no reason for an extra ``USceneComponent``
    in the way, and the actor is never moved -- that is the whole point of the
    path being measured.  Returns ``(actor, component)`` as SPEAR proxies, with
    the actor left at the world origin and an identity rotation, so a pose
    written in ``WorldSpace`` is also the level-space pose the rest of the
    project computes.

    Raises rather than degrading: a silent failure here would leave the robot in
    its bind pose and make the frame-time measurement look wonderful.
    """
    mesh = game.unreal_service.load_object(
        uclass="USkeletalMesh",
        name=asset_path,
        as_unreal_object=True,
    )
    if mesh is None:
        raise RuntimeError(f"skeletal mesh asset could not be loaded: {asset_path!r}")

    # Resolve the class by path: spawn_actor's string form goes through SPEAR's
    # static class-descriptor registry, which does not contain every engine class.
    component_class = game.unreal_service.load_class(
        uclass="USceneComponent",
        name=POSEABLE_COMPONENT_CLASS,
    )
    actor = game.unreal_service.spawn_actor(
        uclass="AActor",
        spawn_parameters={
            "SpawnCollisionHandlingOverride": "AlwaysSpawn",
            "ObjectFlags": ["RF_Transient"],
        },
        as_unreal_object=True,
    )
    component = game.unreal_service.create_scene_component_for_actor(
        owner=actor,
        scene_component_name=component_name,
        uclass=component_class,
        as_unreal_object=True,
    )
    if component is None:
        raise RuntimeError(f"could not create {POSEABLE_COMPONENT_CLASS} on the robot actor")

    component.SetMobility(NewMobility="Movable")
    # ``SetSkinnedAssetAndUpdate`` is the 5.x spelling; ``SetSkeletalMesh`` is
    # the deprecated one and is kept only as a fallback for older builds.
    if getattr(component, "SetSkinnedAssetAndUpdate", None) is not None:
        # ``bReinitPose`` is the parameter name on this function; a wrong
        # keyword makes the RPC fail server-side and leaves the mesh unset,
        # which surfaces much later as "component has no bones".
        component.SetSkinnedAssetAndUpdate(NewMesh=mesh, bReinitPose=True)
    else:  # pragma: no cover - depends on the engine build
        component.SetSkeletalMesh(NewMesh=mesh)

    # Interchange builds a PhysicsAsset for an imported skin, and a simulating
    # skinned component would add a per-frame cost of its own that has nothing
    # to do with the pose path being measured.
    # The parameter is ``NewCastShadow``.  Every keyword below is spelled from
    # the engine header on purpose: the proxy forwards **kwargs by name and a
    # wrong one is not a Python TypeError, it is a server-side failure whose
    # only symptom is that later calls on the same component stop returning
    # anything (observed as "the component has no bones").
    component.SetCastShadow(NewCastShadow=bool(cast_shadow))
    component.SetCollisionEnabled(NewType="NoCollision")
    component.SetGenerateOverlapEvents(bInGenerateOverlapEvents=False)
    component.SetSimulatePhysics(bSimulate=False)

    if stable_name:
        game.unreal_service.set_stable_name_for_actor(actor=actor, stable_name=stable_name)

    names, bone_error = bone_names_of(component)
    if not names:
        raise RuntimeError(
            f"{asset_path!r} produced a poseable component with no bones "
            f"({bone_error}); every bone write would be silently discarded")
    if diagnostics is not None:
        diagnostics.update({
            "asset_path": asset_path,
            "host_actor": stable_name,
            "component_class": POSEABLE_COMPONENT_CLASS,
            "cast_shadow": bool(cast_shadow),
            "bones": names,
        })
    return actor, component


def bone_names_of(component: Any) -> tuple[list[str], str | None]:
    """Return ``(bone names, error)`` for a skinned component.

    ``GetBoneNames(TArray<FName>&)`` is not a ``UFUNCTION`` and so is invisible
    to the proxy; ``GetNumBones``/``GetBoneName`` are the exported pair.  The
    error string exists because "zero bones" has several very different causes
    -- a mesh that never got set, a skeleton that came out empty, a call that
    failed -- and they are indistinguishable from an empty list alone.

    ``GetNumBones`` is tried with and without ``as_value``: an integer return
    goes through different marshalling than an ``FName`` one, and a build that
    asserts on one form still answers the other.
    """
    count = None
    failures = []
    for kwargs in ({"as_value": True}, {}):
        try:
            count = int(component.GetNumBones(**kwargs))
            break
        except Exception as exc:  # reported to the caller
            failures.append(f"{kwargs or 'default'}: {type(exc).__name__}: {exc}")
    if count is None:
        return [], "GetNumBones failed; " + "; ".join(failures)
    if count <= 0:
        asset = None
        try:
            asset = bool(component.GetSkinnedAsset(as_handle=True))
        except Exception as exc:  # reported to the caller
            asset = f"unreadable ({type(exc).__name__}: {exc})"
        return [], f"GetNumBones returned 0 (skinned asset set: {asset})"
    names: list[str] = []
    for index in range(count):
        try:
            names.append(str(component.GetBoneName(BoneIndex=index, as_value=True)))
        except Exception as exc:  # reported to the caller
            return names, f"GetBoneName({index}) failed: {type(exc).__name__}: {exc}"
    return names, None


def missing_bones(requested: Iterable[str], available: Sequence[str]) -> list[str]:
    """Which of ``requested`` have no bone in ``available``.

    A silent mismatch is the failure mode that matters here: the robot renders
    in its bind pose and the frame time looks wonderful.
    """
    have = {str(name) for name in available}
    return [str(name) for name in requested if str(name) not in have]


def read_bone_world_locations(
    component: Any,
    bones: Sequence[str],
    spear_math: Any,
) -> tuple[dict[str, tuple[float, float, float]], str | None]:
    """Read each bone's world translation back out of the component, in cm.

    This is the only end-to-end check that the write path works: the numbers
    come out of the engine rather than out of the same dictionary that was sent
    in.  It cannot validate the glTF bind pose against the MuJoCo frame -- that
    is what comparing against the link anchors is for.
    """
    locations: dict[str, tuple[float, float, float]] = {}
    for bone in bones:
        try:
            transform = component.GetBoneTransformByName(
                BoneName=str(bone),
                BoneSpace="WorldSpace",
                as_value=True,
            )
        except Exception as exc:  # reported to the caller
            return locations, f"{bone}: {type(exc).__name__}: {exc}"
        if not transform:
            return locations, f"{bone}: GetBoneTransformByName returned {transform!r}"
        try:
            matrix = spear_math.to_numpy_matrix_from_spear_transform(
                spear_transform=transform, as_matrix=True)
            locations[str(bone)] = (float(matrix[0, 3]), float(matrix[1, 3]), float(matrix[2, 3]))
        except Exception as exc:  # reported to the caller
            return locations, f"{bone}: could not convert {transform!r}: {exc}"
    return locations, None


def compare_bone_writes(
    bone_locations: Mapping[str, tuple[float, float, float]],
    actor_locations: Mapping[str, tuple[float, float, float]],
) -> dict[str, Any]:
    """Compare bone world positions against the link anchors they should match.

    Both sides are the same MuJoCo pose put through the same
    :func:`rm_sim_spear.pose_sync.to_unreal_pose`, so on a correct bind these
    must agree to well under a millimetre.  A bind-pose mistake -- a missing
    global transform, a sign flip, a mirrored axis -- shows up here as a
    centimetre-scale error on the affected links, instead of as a frame time
    that merely happens to look good.
    """
    rows: dict[str, Any] = {}
    worst = 0.0
    worst_link = None
    for name in sorted(bone_locations):
        if name not in actor_locations:
            continue
        bone = bone_locations[name]
        actor = actor_locations[name]
        delta = max(abs(a - b) for a, b in zip(bone, actor))
        rows[name] = {
            "bone_cm": [round(value, 3) for value in bone],
            "actor_cm": [round(value, 3) for value in actor],
            "delta_cm": round(delta, 3),
        }
        if delta > worst:
            worst, worst_link = delta, name
    return {
        "links": rows,
        "max_delta_cm": round(worst, 3),
        "worst_link": worst_link,
        "matched": len(rows),
    }
