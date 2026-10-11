"""Say what transform an addon-exported MuJoCo scene needs to sit in level coordinates.

``tools/run_spear_sim.py --arena-scene`` takes a world exported by the Blender
``blender_mujoco_terrain`` addon, and the frame that export is written in is not
observable from the file: the addon faithfully writes whatever coordinates the
manifest gave it, and a UE -> Blender conversion that negates y produces a scene
that is the level *reflected*, not merely translated.  Both failures are silent.
A reflected scene still loads, still collides, still looks like an arena -- it
just has the ramps on the wrong side, and the pose bridge then puts the robot
somewhere else entirely, which shows up as a mysteriously slow frame rate
because the capture camera rides the robot into a different part of the map.

So this tool answers the one question that cannot be read off the file: *given
the scene, the level, and Unreal's own measured actor bounds, which transform
brings them together?*

It reports three things:

* whether the objects need ``--arena-scene-mirror-y``,
* the translation from the scene's origin to level coordinates, i.e. the
  ``--world-offset-cm`` the pose bridge needs,
* the ``--arena-scene-spawn-m`` that puts the robot where the level wants it.

Ground truth is ``build/arena/unreal_metadata/scene.json``'s ``world_bounds``,
which is Unreal's own ``actor.get_actor_bounds()``.  Per-actor bounding boxes are
symmetric, so a bbox alone cannot prove handedness -- but an *asymmetric* set of
bboxes can: if every object's y flips, the scene is reflected.

This is now the only frame check the project has, because the other arena source
(``arena.json``) was deleted for being in the wrong one.  Run it on any new
export rather than assuming; see ``docs/rmuc26_asset_review.md``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import mujoco  # noqa: E402
import numpy as np  # noqa: E402

from rm_sim_spear.model import Pose  # noqa: E402
from rm_sim_spear.mujoco_driver import PhysicsOptions  # noqa: E402
from rm_sim_spear.pose_sync import to_unreal_pose  # noqa: E402

DEFAULT_SCENE = REPO_ROOT / "assets" / "rmuc26" / "mjcf" / "scene_rmuc26.xml"
DEFAULT_LEVEL = REPO_ROOT / "build" / "arena" / "unreal_metadata" / "scene.json"

#: Object centres are compared after rounding to this; Unreal's bounds and the
#: addon's decimated visual meshes disagree slightly even when the placement is
#: right (measured: ~1 cm on the objects here).
TOLERANCE_M = 0.05


def unreal_bounds(level_path: Path) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Per-actor world AABB, in metres, from Unreal's own measurement."""
    data = json.loads(level_path.read_text(encoding="utf-8"))
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for actor in data.get("actors", []):
        bounds = actor.get("world_bounds") or {}
        origin, extent = bounds.get("origin_cm"), bounds.get("extent_cm")
        if not origin or not extent:
            continue
        origin, extent = np.asarray(origin, float), np.asarray(extent, float)
        out[actor["label"]] = ((origin - extent) / 100.0, (origin + extent) / 100.0)
    return out


def scene_bounds(model: "mujoco.MjModel", data: "mujoco.MjData"
                 ) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Per-body world AABB of the scene's *mesh* geoms, in metres.

    The addon's meshes are already in the body frame, so a body's box is the
    union of its geoms' boxes; nothing here needs the convex hull.
    """
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for body_id in range(1, model.nbody):
        name = str(model.body(body_id).name)
        lo = np.full(3, np.inf)
        hi = np.full(3, -np.inf)
        for index in range(model.ngeom):
            if int(model.geom_bodyid[index]) != body_id:
                continue
            if int(model.geom_type[index]) != int(mujoco.mjtGeom.mjGEOM_MESH):
                continue
            data_id = int(model.geom_dataid[index])
            start = int(model.mesh_vertadr[data_id])
            stop = start + int(model.mesh_vertnum[data_id])
            matrix = np.zeros(9)
            mujoco.mju_quat2Mat(matrix, np.asarray(model.geom_quat[index], float))
            verts = (np.asarray(model.mesh_vert[start:stop], float)
                     @ matrix.reshape(3, 3).T
                     + np.asarray(data.geom_xpos[index], float))
            if not len(verts):
                continue
            lo = np.minimum(lo, verts.min(axis=0))
            hi = np.maximum(hi, verts.max(axis=0))
        if np.isfinite(lo).all():
            out[name] = (lo, hi)
    return out


def _centres(boxes):
    return {name: (lo + hi) / 2.0 for name, (lo, hi) in boxes.items()}


def scene_to_unreal_cm(point_m: np.ndarray) -> np.ndarray:
    """The *bridge's* own conversion, not a copy of it.

    ``--world-offset-cm`` was originally solved here with an ad-hoc median
    formula.  That formula disagreed with :func:`to_unreal_pose` about the sign
    of y -- the bridge negates y and the formula did not -- so this tool used to
    recommend an offset and a mirror flag that were both wrong, and the arena
    landed 3.2 m from where Unreal draws it.  Calling the bridge makes that
    class of drift impossible.
    """
    pose = Pose(position_m=tuple(float(v) for v in point_m),
                quaternion_wxyz=(1.0, 0.0, 0.0, 0.0))
    return np.asarray(to_unreal_pose(pose).location_cm, float)


def implied_offsets(model, level):
    """``world_offset_cm`` implied by each object, in cm.

    The bridge maps a scene point to ``100 * diag(1, -1, 1) * p``, so the offset
    is whatever else brings that onto Unreal's own actor centre.  A correct
    import setting makes every object imply the *same* offset -- that is the
    whole test, and it is one number per axis, so it is hard to be fooled.
    """
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    offsets, targets = {}, {}
    for name, centre in _centres(scene_bounds(model, data)).items():
        actor = name[len("static_"):] if name.startswith("static_") else name
        reference = level.get(actor)
        if reference is None:
            continue
        target = 100.0 * (reference[0] + reference[1]) / 2.0
        offsets[actor] = target - scene_to_unreal_cm(np.asarray(centre))
        targets[actor] = target
    return offsets, targets


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scene", type=Path, default=DEFAULT_SCENE)
    parser.add_argument("--level", type=Path, default=DEFAULT_LEVEL,
                        help="unreal_metadata/scene.json with per-actor world_bounds")
    parser.add_argument("--spawn-m", type=float, nargs=3, default=None,
                        metavar=("X", "Y", "Z"),
                        help="level spawn in metres (default: the arena's own spawn)")
    parser.add_argument("--json", action="store_true", help="emit one JSON event")
    args = parser.parse_args()

    level = unreal_bounds(args.level)
    print(f"level: {len(level)} actors with world_bounds from {args.level.name}")

    spawn_m = np.asarray(args.spawn_m if args.spawn_m is not None
                         else (0.0, 0.0, -1.3765))

    for tag, mirror in (("as exported", False), ("with mirror-y", True)):
        options = PhysicsOptions(arena_scene=args.scene, arena_scene_mirror_y=mirror)
        model = options._arena_scene_base_spec().compile()
        implicated, targets = implied_offsets(model, level)
        print(f"\n=== {tag} ===")
        print(f"{'object':<46} {'implied --world-offset-cm':>28}")
        if not implicated:
            print("  (no object in the scene matches an actor in the level)")
            continue
        for actor in sorted(implicated):
            offset = implicated[actor]
            print(f"{actor[:46]:<46} "
                  f"[{offset[0]:+9.1f} {offset[1]:+9.1f} {offset[2]:+9.1f}]")
        stacked = np.asarray(list(implicated.values()))
        median = np.median(stacked, axis=0)
        residual = np.abs(stacked - median).max(axis=1)
        inside = int((residual < TOLERANCE_M * 100.0).sum())
        print(f"{'-- median offset':<46} [{median[0]:+9.1f} {median[1]:+9.1f} "
              f"{median[2]:+9.1f}]   {inside}/{len(implicated)} objects agree "
              f"within {TOLERANCE_M*100:.0f} cm")
        outliers = [name for name, bad in zip(implicated, residual)
                    if bad >= TOLERANCE_M * 100.0]
        # A handful of actors legitimately do not line up: the addon rebuilds the
        # terrain and cuts pieces out of it, so ``SM_MERGED_Actor_445``'s
        # decimated visual is not the same shape as Unreal's merged actor.  A
        # quorum is the honest test -- a wrong hypothesis does not get 80% of the
        # level to agree by accident.
        quorum = max(3, int(0.75 * len(implicated)))
        if outliers:
            print(f"  (not lining up: {', '.join(outliers)})")
        if inside >= quorum:
            # The scene point that the bridge maps onto the level spawn.  The
            # bridge is 100*diag(1,-1,1)*p + offset, so undoing it needs the
            # same matrix: it is its own inverse.
            scene_spawn = np.array([1.0, -1.0, 1.0]) * (spawn_m - median / 100.0)
            print("\n  >>> this is the transform to use")
            print("      --no-arena-scene-mirror-y" if not mirror
                  else "      --arena-scene-mirror-y  (NOT the default)")
            print(f"      --arena-scene-spawn-m "
                  f"{scene_spawn[0]:.4f} {scene_spawn[1]:.4f} {scene_spawn[2]:.4f}")
            print(f"      --world-offset-cm "
                  f"{median[0]:.2f} {median[1]:.2f} {median[2]:.2f}")
            if args.json:
                print(json.dumps({
                    "event": "arena_scene_frame", "scene": str(args.scene),
                    "mirror_y": mirror,
                    "world_offset_cm": [round(float(v), 2) for v in median],
                    "arena_scene_spawn_m": [round(float(v), 4) for v in scene_spawn],
                    "objects_agreeing": inside, "objects_compared": len(implicated),
                    "outliers": outliers,
                }, ensure_ascii=False))
            return
        print("  (objects disagree -- this hypothesis is wrong)")


if __name__ == "__main__":
    main()
