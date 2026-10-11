"""Export the level's static arena geometry and Unreal's own per-actor bounds.

Run with the editor commandlet:

    UnrealEditor-Cmd <project>.uproject -run=pythonscript \
        -script=tools/export_arena_geometry.py -unattended -nop4 -nosplash

Output:

    build/arena/unreal_metadata/scene.json              # actor -> mesh -> world transform
    build/arena/unreal_geometry/raw/*.obj               # exactly what UE wrote

**What this is still for.**  ``scene.json`` carries
``actor.get_actor_bounds()`` for every actor -- Unreal's *own* measurement of
where each object is -- and that is the ground truth
``tools/check_arena_scene.py`` uses to decide which frame an exported arena is
in.  Nothing else in the repo can produce it.  The OBJ files are still written
in full so that the geometry and the bounds come from the same run, but the
in-repo builder that used to read them (``pipeline/arena_scene.py``) has been
deleted; see ``docs/rmuc26_asset_review.md``.

The OBJ files are left **exactly** as Unreal wrote them, including the y/z
exchange the exporter applies: that conversion belongs to whoever *reads* the
geometry, and writing a second, "corrected" copy of 800 MB would not have made
the convention any clearer.

Two conventions matter:

* **Unreal writes the OBJ with y and z exchanged**, so ``v a b c`` means
  ``ue = (a, c, b)``.
* **No axis is negated here.**  Unreal is left-handed and MuJoCo is right-handed,
  so a faithful copy is a mirror image -- which is fine, as long as everyone
  agrees *where* the mirror is applied.  This repo applies it in exactly one
  place: :func:`rm_sim_spear.pose_sync.to_unreal_pose`, which converts with
  ``100 * diag(1, -1, 1)``.  So the MuJoCo world is the level reflected in y,
  and anything placed into that world has to be in the same frame.  The Blender
  export in ``assets/rmuc26`` is; the deleted ``arena.json`` was not, which is
  why it is gone.  (This docstring previously claimed the bridge was
  axis-preserving.  It is not, and that error is what hid the disagreement.)

Every actor in ``scene.json`` carries ``world_bounds``; ``check_arena_scene.py``
compares an export against them, and an off-by-one in the y/z swap shows up
there as a permuted or inflated extent rather than as a plausible-looking wall.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import unreal  # noqa: E402 - must follow the repo-root sys.path insert

DEFAULT_LEVEL = "/Game/creo_stp"
#: Under ``build/`` on purpose: the OBJ intermediates are hundreds of megabytes
#: of regenerable geometry, and ``build/`` is gitignored.  Only
#: ``unreal_metadata/scene.json`` is still consumed (by
#: ``tools/check_arena_scene.py``); the OBJ trees can be deleted at any time.
DEFAULT_OUTPUT = "build/arena"

#: Content the robot owns.  The arena is "everything else that is static", which
#: is the same rule the level survey (tools/inspect_level_references.py) uses to
#: separate the two; keep the two lists in step.
DEFAULT_EXCLUDE = (
    "/Game/RMSim/",
    "/Game/armor_tune/",
    "/SpContent/",
)


def _vector_like(value) -> list[float] | None:
    """``[x, y, z]`` from a UE Vector, whatever accessor this build exposes."""
    if value is None:
        return None
    out = []
    for axis in ("x", "y", "z"):
        try:
            out.append(float(getattr(value, axis)))
        except AttributeError:
            try:
                out.append(float(value.get_editor_property(axis)))
            except Exception:  # noqa: BLE001 - unknown binding, report as missing
                return None
    return out


def _quaternion_like(value) -> list[float] | None:
    """``[x, y, z, w]`` from a UE Quat."""
    if value is None:
        return None
    out = []
    for axis in ("x", "y", "z", "w"):
        try:
            out.append(float(getattr(value, axis)))
        except AttributeError:
            try:
                out.append(float(value.get_editor_property(axis)))
            except Exception:  # noqa: BLE001 - unknown binding, report as missing
                return None
    return out


def _transform_like(transform) -> dict | None:
    if transform is None:
        return None
    return {
        "location_cm": _vector_like(transform.translation),
        "rotation_quaternion_xyzw": _quaternion_like(transform.rotation),
        "scale3d": _vector_like(transform.scale3d),
    }


def _mesh_package(mesh) -> str | None:
    if mesh is None:
        return None
    try:
        return str(mesh.get_path_name()).split(".", 1)[0]
    except Exception:  # noqa: BLE001 - cooked/stripped objects can lack a path
        return None


def _export_mesh_exactly_once(asset_registry, package: str, raw_path: Path) -> list[str]:
    """Export one StaticMesh asset to OBJ; returns the exporter's error list."""
    errors: list[str] = []
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    # get_asset_by_object_path() wants the object path (``/Game/Dir/Name.Name``),
    # not the package path (``/Game/Dir/Name``); passing the package returns
    # None and the exporter then trips an assertion inside UnrealExporter.
    object_path = f"{package}.{package.rsplit('/', 1)[-1]}"
    asset = asset_registry.get_asset_by_object_path(object_path=object_path).get_asset()
    if asset is None:
        return [f"no asset for object path {object_path!r}"]
    task = unreal.AssetExportTask()
    task.set_editor_property(name="automated", value=True)
    task.set_editor_property(name="errors", value=errors)
    task.set_editor_property(name="exporter", value=None)
    task.set_editor_property(name="filename", value=str(raw_path))
    task.set_editor_property(name="ignore_object_list", value=[])
    task.set_editor_property(name="object", value=asset)
    task.set_editor_property(name="options", value=None)
    task.set_editor_property(name="prompt", value=False)
    task.set_editor_property(name="replace_identical", value=True)
    task.set_editor_property(name="selected", value=False)
    task.set_editor_property(name="use_file_archive", value=False)
    task.set_editor_property(name="write_empty_files", value=False)
    unreal.Exporter.run_asset_export_task(task=task)
    return [str(error) for error in errors]


#: (trace channel, complex) settings tried in order.  Imported Datasmith
#: meshes usually have no *simple* collision, so a per-triangle trace is the one
#: that works; which channel is meaningful is project configuration, so it is
#: discovered rather than assumed.
_PROBE_CANDIDATES = (
    ("TRACE_TYPE_QUERY1", True),
    ("TRACE_TYPE_QUERY3", True),
    ("TRACE_TYPE_QUERY2", True),
    ("TRACE_TYPE_QUERY1", False),
    ("TRACE_TYPE_QUERY3", False),
)


def _cast(world, x: float, y: float, z_top: float, z_bottom: float,
          channel: str, complex_trace: bool):
    """One downward trace; returns the hit z or None."""
    start = unreal.Vector(x=x, y=y, z=z_top)
    end = unreal.Vector(x=x, y=y, z=z_bottom)
    try:
        hit = unreal.SystemLibrary.line_trace_single(
            world, start, end, getattr(unreal.TraceTypeQuery, channel),
            complex_trace, [], unreal.DrawDebugTrace.NONE, True)
    except Exception:  # noqa: BLE001 - unknown binding must not sink the run
        return None
    if hit is None:
        return None
    try:
        if not hit.get_editor_property("blocking_hit"):
            return None
        return float(hit.get_editor_property("location").z)
    except Exception:  # noqa: BLE001 - unknown hit layout
        return None


def _probe_ground(actors: list[dict], step_cm: float) -> dict | None:
    """Raycast the level straight down on a grid and record where it hits.

    This is the authoritative answer to "where is the floor", and it is much
    better than anything derived from the mesh: the arena is not one level (its
    centre is a stacked structure spanning 1.5 m), and a few huge triangles can
    cover most of the field, so a purely geometric vote either misses the floor
    or picks the wrong surface.  A trace asks the level itself.

    Returns the hit heights plus the grid they belong to, or ``None`` when no
    setting hits anything (imported meshes commonly carry no collision at all).
    """
    boxes = [a["world_bounds"] for a in actors if a.get("world_bounds")]
    if not boxes:
        return None
    lower = [min(b["origin_cm"][axis] - b["extent_cm"][axis] for b in boxes)
             for axis in range(3)]
    upper = [max(b["origin_cm"][axis] + b["extent_cm"][axis] for b in boxes)
             for axis in range(3)]
    z_top = upper[2] + 500.0
    z_bottom = lower[2] - 500.0

    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()

    # Discover a working setting on a coarse sub-grid before tracing all of it.
    probe_points = []
    for iy in range(8):
        for ix in range(12):
            probe_points.append((
                lower[0] + (ix + 0.5) * (upper[0] - lower[0]) / 12.0,
                lower[1] + (iy + 0.5) * (upper[1] - lower[1]) / 8.0))
    chosen = None
    for channel, complex_trace in _PROBE_CANDIDATES:
        hits = sum(1 for x, y in probe_points
                   if _cast(world, x, y, z_top, z_bottom, channel, complex_trace) is not None)
        print(json.dumps({"event": "arena_ground_probe_candidate", "channel": channel,
                          "complex": complex_trace, "hits": hits,
                          "of": len(probe_points)}, ensure_ascii=False), flush=True)
        if hits and chosen is None:
            chosen = (channel, complex_trace)
    if chosen is None:
        return None
    channel, complex_trace = chosen

    n_x = int((upper[0] - lower[0]) / step_cm) + 1
    n_y = int((upper[1] - lower[1]) / step_cm) + 1
    if n_x * n_y > 200000:  # a sanity cap; the default grid is ~2000 traces
        step_cm = max(step_cm, ((upper[0] - lower[0]) * (upper[1] - lower[1]) / 200000) ** 0.5)
        n_x = int((upper[0] - lower[0]) / step_cm) + 1
        n_y = int((upper[1] - lower[1]) / step_cm) + 1

    heights: list[float | None] = []
    missed = 0
    for iy in range(n_y):
        y = lower[1] + iy * step_cm
        for ix in range(n_x):
            z = _cast(world, lower[0] + ix * step_cm, y, z_top, z_bottom,
                      channel, complex_trace)
            if z is None:
                missed += 1
            heights.append(z)
    if missed == len(heights):
        return None
    return {
        "trace_channel": channel,
        "trace_complex": complex_trace,
        "step_cm": step_cm,
        "shape": [n_y, n_x],
        "origin_cm": [lower[0], lower[1]],
        "z_top_cm": upper[2],
        "missed": missed,
        "heights_cm": heights,
    }


def _component_placements(component) -> list[dict]:
    """World transforms of one static mesh component: 1, or N when instanced."""
    placements = []
    try:
        count = int(component.get_instance_count())
    except Exception:  # noqa: BLE001 - a plain component has no instance count
        count = 0
    if count > 0:
        for index in range(count):
            try:
                transform = component.get_instance_transform(index)
            except Exception:  # noqa: BLE001 - ISM without exported instances
                continue
            placements.append({"kind": "instanced", "instance": index,
                               **_transform_like(transform)})
    else:
        placements.append({"kind": "static", **_transform_like(
            component.get_world_transform())})
    return placements


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--level", default=os.environ.get("RMSIM_ARENA_LEVEL", DEFAULT_LEVEL))
    parser.add_argument("--output", type=Path,
                        default=Path(os.environ.get("RMSIM_ARENA_OUTPUT", DEFAULT_OUTPUT)))
    parser.add_argument(
        "--exclude", default=os.environ.get("RMSIM_ARENA_EXCLUDE", ",".join(DEFAULT_EXCLUDE)),
        help="comma-separated mesh package prefixes that are NOT the arena")
    parser.add_argument(
        "--max-meshes", type=int, default=int(os.environ.get("RMSIM_ARENA_MAX_MESHES", "0")),
        help="stop after exporting this many mesh assets (0 = all); a smoke-test knob")
    parser.add_argument(
        "--ground-probe-step-cm", type=float,
        default=float(os.environ.get("RMSIM_ARENA_PROBE_STEP_CM", "50")),
        help="grid spacing for the downward ground raycast")
    parser.add_argument(
        "--ground-probe", action=argparse.BooleanOptionalAction, default=True,
        help="raycast the level for ground heights; off means the builder has to "
             "derive them from the mesh, which is much less reliable")
    parser.add_argument(
        "--metadata-only", action="store_true",
        default=os.environ.get("RMSIM_ARENA_METADATA_ONLY", "") not in ("", "0"),
        help="skip the OBJ export and refresh scene.json alone; the geometry pass "
             "is ~3 GB and minutes long, the metadata pass is seconds")
    args = parser.parse_args()

    exclude = tuple(p for p in args.exclude.split(",") if p)
    output = args.output if args.output.is_absolute() else REPO_ROOT / args.output
    metadata_dir = output / "unreal_metadata"
    raw_dir = output / "unreal_geometry" / "raw"

    if not unreal.EditorLoadingAndSavingUtils.load_map(args.level):
        raise RuntimeError(f"failed to load level: {args.level}")

    subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    registry = unreal.AssetRegistryHelpers.get_asset_registry()

    actors_out: list[dict] = []
    per_mesh_instances: dict[str, int] = {}
    skipped_robot = 0
    failures: list[str] = []

    for actor in subsystem.get_all_level_actors():
        try:
            components = actor.get_components_by_class(unreal.StaticMeshComponent)
        except Exception as exc:  # noqa: BLE001 - keep going, report the count
            failures.append(f"{actor.get_actor_label()}: {type(exc).__name__}: {exc}")
            continue
        entries = []
        for component in components:
            package = _mesh_package(component.static_mesh)
            if package is None:
                continue
            if any(package.startswith(prefix) for prefix in exclude):
                skipped_robot += 1
                continue
            placements = _component_placements(component)
            if not placements:
                continue
            per_mesh_instances[package] = per_mesh_instances.get(package, 0) + len(placements)
            entries.append({"component": str(component.get_name()), "mesh": package,
                            "placements": placements})
        if not entries:
            continue
        try:
            label = str(actor.get_actor_label())
        except Exception:  # noqa: BLE001 - label is a nicety, not a key
            label = str(actor.get_name())
        try:
            folder = str(actor.get_folder_path())
        except Exception:  # noqa: BLE001 - folder is a nicety, not a key
            folder = ""
        # Unreal's own world bounds for the whole actor.  This is the
        # independent measurement every derived bbox is checked against: if the
        # pipeline's world bbox disagrees with this, the transform composition
        # is wrong, not the arena.
        bounds = None
        try:
            origin, extent = actor.get_actor_bounds(False)
            bounds = {
                "origin_cm": _vector_like(origin),
                "extent_cm": _vector_like(extent),
            }
        except Exception:  # noqa: BLE001 - not every actor implements it
            bounds = None
        actors_out.append({
            "label": label,
            "folder": folder,
            "class": str(actor.get_class().get_name()),
            "world_bounds": bounds,
            "components": entries,
        })

    # Export each distinct mesh asset once.  Sorting keeps the run reproducible.
    packages = sorted(per_mesh_instances)
    if args.max_meshes > 0:
        packages = packages[:args.max_meshes]
    meshes_out: dict[str, dict] = {}
    export_failures: list[str] = []
    for index, package in enumerate(packages, start=1):
        suffix = package.lstrip("/").replace("/", os.sep) + ".obj"
        raw_path = raw_dir / suffix
        if args.metadata_only:
            # Keep whatever a previous geometry pass wrote; only the metadata
            # is being refreshed.
            if raw_path.is_file():
                meshes_out[package] = {
                    "obj": str(raw_path.relative_to(output)),
                }
            continue
        errors = _export_mesh_exactly_once(registry, package, raw_path)
        if errors or not raw_path.is_file():
            export_failures.append(f"{package}: {errors or 'no file written'}")
            continue
        meshes_out[package] = {
            "obj": str(raw_path.relative_to(output)),
        }
        if index % 25 == 0:
            print(json.dumps({"event": "arena_export_progress", "exported": index,
                              "of": len(packages)}, ensure_ascii=False), flush=True)

    ground_probe = None
    if args.ground_probe:
        ground_probe = _probe_ground(actors_out, args.ground_probe_step_cm)
        print(json.dumps({"event": "arena_ground_probe",
                          "probed": None if ground_probe is None else len(ground_probe["heights_cm"]),
                          "missed": None if ground_probe is None else ground_probe["missed"]},
                         ensure_ascii=False), flush=True)

    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "scene.json").write_text(json.dumps({
        "level": args.level,
        "units": "cm",
        "axes": "unreal world: x forward, y right, z up, left-handed",
        "convention": "numerical parity with Unreal; no axis is negated. "
                      "See this file's module docstring.",
        "exclude_prefixes": list(exclude),
        "actor_count": len(actors_out),
        "distinct_meshes": len(meshes_out),
        "instances": sum(per_mesh_instances.get(p, 0) for p in meshes_out),
        "actors": actors_out,
        "meshes": meshes_out,
        "export_failures": export_failures,
        "component_read_failures": failures,
        "ground_probe": ground_probe,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "event": "arena_export",
        "level": args.level,
        "output": str(output),
        "scene_json": str(metadata_dir / "scene.json"),
        "actors": len(actors_out),
        "distinct_meshes": len(meshes_out),
        "instances": sum(per_mesh_instances.get(p, 0) for p in meshes_out),
        "robot_components_skipped": skipped_robot,
        "export_failures": len(export_failures),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
