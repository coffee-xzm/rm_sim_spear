"""Import the URDF robot visuals into the UE level that SPEAR loads.

Run with the editor commandlet (no PIE, no rendering):

    UnrealEditor-Cmd <project>.uproject -run=pythonscript \
        -script=tools/import_urdf_visuals.py -unattended -nop4 -nosplash

It reads ``generated/urdf/<robot>/manifest.json`` (link visuals with their
URDF origins), converts every *unique* STL once into a UE-space OBJ using
``pipeline.urdf_visual_meshes``, imports those OBJs as StaticMesh assets under
``/Game/RMSim/<robot>/Meshes``, and places the robot in the map:

* one anchor actor per URDF link (label = link name, tag
  ``MuJoCoBody=<link>``), attached to its URDF parent joint with the joint
  origin converted to UE centimetres, and
* one StaticMeshActor per visual, attached to its link anchor with the visual
  origin converted the same way.

The link anchors are what the runtime binder drives: ``apply_scene_binding_plan``
resolves them by the ``MuJoCoBody=`` tag (SPEAR matches stable names by UObject
name in ``-game``, so an editor label alone is not addressable), then pushes the
MuJoCo link pose into each one every frame.

A shared StaticMesh per unique STL (not one merged mesh per link) is deliberate:
the four wheel links reference 128 visuals that come from only 10 files, so
merging them would bake 16.8 M triangles into the level.  Sharing the 210 unique
meshes keeps the GPU footprint at the ~2.2 M triangles actually authored.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.urdf_visual_meshes import origin_matrix, read_stl, write_obj  # noqa: E402
from pipeline.visual_override import (  # noqa: E402
    SOURCE_PLACEHOLDER,
    SOURCE_UE_ASSET,
    SOURCE_URDF,
    asset_placements,
    first_visual_origin,
    load_overrides,
    visual_source,
)

DEFAULT_MANIFEST = REPO_ROOT / "generated" / "urdf" / "总装" / "manifest.json"
DEFAULT_LEVEL = "/Game/creo_stp"
DEFAULT_PACKAGE = "/Game/RMSim/总装"
# The armor plates come from the level's own armor module (via the per-link
# ``visual_source: ue_asset`` override), so the URDF's own armor meshes -- which
# carry no material at all -- stay out by default.  A non-``urdf`` override
# outranks this list.
DEFAULT_SKIP_LINKS = "armor_link_1,armor_link_2,armor_link_3,armor_link_4"


def parse_args(argv=None):
    """Parse the importer's options.

    The commandlet passes the engine's own switches through ``sys.argv``, so
    unknown arguments are ignored; every option can also come from the
    environment (``RMSIM_IMPORT_*``), which is how the repo's other UE Python
    tools are configured from a shell.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path,
                        default=Path(os.environ.get("RMSIM_MANIFEST", DEFAULT_MANIFEST)))
    parser.add_argument("--level", default=os.environ.get("RMSIM_LEVEL", DEFAULT_LEVEL))
    parser.add_argument("--package", default=os.environ.get("RMSIM_IMPORT_PACKAGE", DEFAULT_PACKAGE))
    parser.add_argument("--obj-dir", type=Path,
                        default=Path(os.environ.get("RMSIM_IMPORT_OBJ_DIR", "/tmp/rmsim_urdf_obj")))
    parser.add_argument("--report", type=Path,
                        default=Path(os.environ.get("RMSIM_IMPORT_REPORT", "/tmp/rmsim_urdf_import.json")))
    parser.add_argument("--skip-links",
                        default=os.environ.get("RMSIM_IMPORT_SKIP_LINKS", DEFAULT_SKIP_LINKS))
    parser.add_argument("--limit-links",
                        default=os.environ.get("RMSIM_IMPORT_LIMIT_LINKS", ""),
                        help="comma separated subset, for a smoke test")
    parser.add_argument("--instanced-visuals", action="store_true",
                        help="place one HierarchicalInstancedStaticMeshComponent per "
                             "(link, mesh) instead of one StaticMeshActor per visual: "
                             "955 -> 248 transformed components, no geometry "
                             "duplication (docs/render_budget.md 17)")
    parser.add_argument("--limit-visuals", type=int,
                        default=int(os.environ.get("RMSIM_IMPORT_LIMIT_VISUALS", "0")),
                        help="max visuals per link (0 = all)")
    parser.add_argument("--no-save", action="store_true",
                        default=bool(os.environ.get("RMSIM_IMPORT_NO_SAVE")),
                        help="do not save assets or the level")
    parser.add_argument("--force-reimport", action="store_true",
                        default=bool(os.environ.get("RMSIM_IMPORT_FORCE")),
                        help="delete existing mesh assets and import them again")
    parser.add_argument("--overrides", type=Path,
                        default=Path(os.environ.get(
                            "RMSIM_IMPORT_OVERRIDES",
                            REPO_ROOT / "generated" / "urdf" / "总装" / "ue_overrides.json")),
                        help="ue_overrides.json whose per-link visual_source / ue_asset "
                             "entries replace the URDF meshes (docs/urdf_workflow.md); "
                             "a missing file means every link uses the URDF mesh")
    args, _unknown = parser.parse_known_args(argv)
    return args


def ue_transform(xyz_m, rpy_rad):
    """URDF metres + RPY radians -> UE centimetres + quaternion (Y mirrored)."""
    import unreal

    location = unreal.Vector(100.0 * xyz_m[0], -100.0 * xyz_m[1], 100.0 * xyz_m[2])
    # R_ue = D . R_urdf . D with D = diag(1, -1, 1); the same conjugation applies
    # to the rotation matrix that the URDF origin already is.
    matrix = origin_matrix((0.0, 0.0, 0.0), rpy_rad)[:3, :3]
    flip = [[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, 1.0]]
    converted = [[0.0] * 3 for _ in range(3)]
    for row in range(3):
        for col in range(3):
            converted[row][col] = sum(
                flip[row][k] * matrix[k][l] * flip[l][col] for k in range(3) for l in range(3)
            )
    # Build the quaternion from the converted rotation matrix (Shepperd's method)
    # instead of relying on a URDF->UE euler convention.
    trace = converted[0][0] + converted[1][1] + converted[2][2]
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * scale
        x = (converted[2][1] - converted[1][2]) / scale
        y = (converted[0][2] - converted[2][0]) / scale
        z = (converted[1][0] - converted[0][1]) / scale
    elif converted[0][0] > converted[1][1] and converted[0][0] > converted[2][2]:
        scale = math.sqrt(1.0 + converted[0][0] - converted[1][1] - converted[2][2]) * 2.0
        w = (converted[2][1] - converted[1][2]) / scale
        x = 0.25 * scale
        y = (converted[0][1] + converted[1][0]) / scale
        z = (converted[0][2] + converted[2][0]) / scale
    elif converted[1][1] > converted[2][2]:
        scale = math.sqrt(1.0 + converted[1][1] - converted[0][0] - converted[2][2]) * 2.0
        w = (converted[0][2] - converted[2][0]) / scale
        x = (converted[0][1] + converted[1][0]) / scale
        y = 0.25 * scale
        z = (converted[1][2] + converted[2][1]) / scale
    else:
        scale = math.sqrt(1.0 + converted[2][2] - converted[0][0] - converted[1][1]) * 2.0
        w = (converted[1][0] - converted[0][1]) / scale
        x = (converted[0][2] + converted[2][0]) / scale
        y = (converted[1][2] + converted[2][1]) / scale
        z = 0.25 * scale
    quat = unreal.Quat(x, y, z, w)
    quat.normalize()
    return location, quat


def attachment_rule(unreal, names=("KEEP_WORLD", "KEEP_RELATIVE", "SNAP_TO_TARGET")):
    """Return any usable ``EAttachmentRule`` member for this binding build.

    The member names differ between UE Python builds (``KEEP_WORLD`` here,
    ``KEEP_WORLD_TRANSFORM`` in older ones).  The caller overwrites the relative
    transform right after attaching, so any rule gives the correct result.
    """
    for name in names:
        rule = getattr(unreal.AttachmentRule, name, None)
        if rule is not None:
            return rule
    raise RuntimeError(f"no usable AttachmentRule member: {dir(unreal.AttachmentRule)}")


def expected_asset_bounds(obj_path):
    """UE-frame bounds a correctly imported OBJ must have: D . 100 . v_urdf."""
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    with open(obj_path, "r", encoding="utf-8") as handle:
        for line in handle:
            if not line.startswith("v "):
                continue
            parts = line.split()
            values = [float(parts[1]), -float(parts[2]), float(parts[3])]
            for axis in range(3):
                lo[axis] = min(lo[axis], values[axis])
                hi[axis] = max(hi[axis], values[axis])
    return [(lo[i] + hi[i]) / 2.0 for i in range(3)], [(hi[i] - lo[i]) / 2.0 for i in range(3)]


def quat_to_rotator(unreal, quat):
    """Convert a Quat to a Rotator across UE Python binding differences."""
    for attempt in (
        lambda: quat.rotator(),
        lambda: unreal.KismetMathLibrary.quat_rotator(quat),
        lambda: unreal.MathLibrary.quat_rotator(quat),
    ):
        try:
            return attempt()
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("this build exposes no Quat -> Rotator conversion")


def main(argv=None):
    args = parse_args(argv)
    import unreal

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    robot = manifest.get("robot") or Path(args.manifest).parent.name
    package = args.package.rstrip("/")
    mesh_package = f"{package}/Meshes"
    skip_links = {name for name in args.skip_links.split(",") if name}
    if args.limit_links:
        wanted = {name for name in args.limit_links.split(",") if name}
    else:
        wanted = None

    links = manifest["links"]
    joints = {joint["child"]: joint for joint in manifest.get("joints", [])}
    overrides = load_overrides(args.overrides)
    override_sources = {name: visual_source(overrides.get(name)) for name in
                        (link["name"] for link in links)}

    # 1. Convert each unique STL into one OBJ (UE centimetres, Y mirrored).
    args.obj_dir.mkdir(parents=True, exist_ok=True)
    unique_paths = sorted({v["mesh_path"] for link in links for v in link["visuals"]})
    obj_for_source: dict[str, str] = {}
    for index, source in enumerate(unique_paths):
        obj_path = args.obj_dir / f"rmsim_mesh_{index:03d}.obj"
        if args.force_reimport or not obj_path.is_file():
            triangles = read_stl(source)
            # Scale to centimetres only. The Y mirror is deliberately NOT baked
            # in: Interchange's OBJ translator already mirrors Y on import
            # (verified by comparing asset bounds against the OBJ bounds), so an
            # unmirrored OBJ lands in the UE frame that the placement transform
            # below assumes.
            converted = triangles.copy()
            converted *= 100.0
            write_obj(obj_path, converted)
        obj_for_source[source] = str(obj_path)

    # 2. Import the OBJs as StaticMesh assets.
    def set_prop(obj, name, value):
        """Set an import-task property across UE Python binding differences."""
        try:
            setattr(obj, name, value)
            return True
        except Exception:  # noqa: BLE001
            try:
                obj.set_editor_property(name, value)
                return True
            except Exception:  # noqa: BLE001
                return False

    tasks = []
    for source, obj_path in obj_for_source.items():
        asset_path = f"{mesh_package}/{Path(obj_path).stem}"
        if args.force_reimport and unreal.EditorAssetLibrary.does_asset_exist(asset_path):
            unreal.EditorAssetLibrary.delete_asset(asset_path)
        if not unreal.EditorAssetLibrary.does_asset_exist(asset_path):
            task = unreal.AssetImportTask()
            set_prop(task, "filename", obj_path)
            set_prop(task, "destination_path", mesh_package)
            set_prop(task, "destination_name", Path(obj_path).stem)
            set_prop(task, "automated_import_should_detect_type", True)
            set_prop(task, "replace_existing", True)
            set_prop(task, "save", not args.no_save)
            tasks.append(task)
    if tasks:
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks(tasks)

    if tasks and not args.no_save:
        # Persist the mesh assets before touching the level: importing 210 dense
        # CAD meshes takes minutes, and a placement failure must not lose them.
        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)

    imported = {
        source: unreal.EditorAssetLibrary.load_asset(f"{mesh_package}/{Path(obj).stem}")
        for source, obj in obj_for_source.items()
    }
    missing = sorted(source for source, mesh in imported.items() if mesh is None)

    # Self-check that the imported geometry really is in the UE frame the
    # placement transform below assumes, instead of trusting the importer.
    geometry_check = {}
    for source, mesh in sorted(imported.items()):
        if mesh is None:
            continue
        obj_path = obj_for_source[source]
        expected_origin, expected_extent = expected_asset_bounds(obj_path)
        try:
            bounds = mesh.get_bounds()
            actual_origin = [bounds.origin.x, bounds.origin.y, bounds.origin.z]
            actual_extent = [bounds.box_extent.x, bounds.box_extent.y, bounds.box_extent.z]
        except Exception as exc:  # noqa: BLE001
            geometry_check = {"error": f"{type(exc).__name__}: {exc}"}
            break
        geometry_check = {
            "mesh": Path(obj_path).stem,
            "expected_origin": [round(v, 3) for v in expected_origin],
            "actual_origin": [round(v, 3) for v in actual_origin],
            "expected_extent": [round(v, 3) for v in expected_extent],
            "actual_extent": [round(v, 3) for v in actual_extent],
            "origin_delta_cm": round(max(abs(a - b) for a, b in zip(expected_origin, actual_origin)), 3),
            "extent_delta_cm": round(max(abs(a - b) for a, b in zip(expected_extent, actual_extent)), 3),
        }
        geometry_check["ok"] = (geometry_check["origin_delta_cm"] < 0.5
                                and geometry_check["extent_delta_cm"] < 0.5)
        break

    # 3. Place the robot in the level.
    if not unreal.EditorLoadingAndSavingUtils.load_map(args.level):
        raise RuntimeError(f"failed to load level: {args.level}")
    subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    # Idempotency: drop a previous import of this robot before re-adding it.
    removed = 0
    for actor in list(subsystem.get_all_level_actors()):
        try:
            tags = [str(tag) for tag in actor.tags]
        except Exception:  # noqa: BLE001
            continue
        if f"RMSimRobot={robot}" in tags:
            subsystem.destroy_actor(actor)
            removed += 1

    link_actors = {}
    placed_visuals = 0
    placement_errors: list[str] = []
    report_links = []
    for link in links:
        name = link["name"]
        source = override_sources.get(name, SOURCE_URDF)
        # A link whose visual comes from a UE asset (or is a placeholder) still
        # needs its anchor: the runtime pose loop drives that actor, and the
        # override attaches to it.  Such a link also outranks ``--skip-links``,
        # which exists to suppress the *URDF* visuals of that link.
        if (name in skip_links and source == SOURCE_URDF) or (
                wanted is not None and name not in wanted):
            continue
        try:
            anchor = subsystem.spawn_actor_from_class(unreal.Actor, unreal.Vector(0, 0, 0))
            anchor.set_actor_label(name)
            anchor.tags = [f"MuJoCoBody={name}", f"RobotLink={name}", f"RMSimRobot={robot}"]
            joint = joints.get(name)
            if joint is not None:
                location, quat = ue_transform(joint["origin"]["xyz"], joint["origin"]["rpy"])
                parent = link_actors.get(joint["parent"])
                if parent is not None:
                    rule = attachment_rule(unreal)
                    anchor.attach_to_actor(parent, "", rule, rule, rule)
                anchor.root_component.set_relative_location_and_rotation(
                    location, quat_to_rotator(unreal, quat), False, False)
            link_actors[name] = anchor
        except Exception as exc:  # noqa: BLE001
            placement_errors.append(f"anchor {name}: {type(exc).__name__}: {exc}")
            continue

        if source == SOURCE_UE_ASSET:
            placements = asset_placements(
                overrides.get(name), first_visual_origin(link))
            placed = 0
            for index, placement in enumerate(placements):
                try:
                    location, quat = ue_transform(placement.xyz_m, placement.rpy_rad)
                    child = subsystem.spawn_actor_from_class(
                        unreal.StaticMeshActor, unreal.Vector(0, 0, 0))
                    child.set_actor_label(placement.label or f"{name}_asset_{index:03d}")
                    child.tags = [f"RMSimRobot={robot}", f"RMSimLink={name}"]
                    mesh = unreal.EditorAssetLibrary.load_asset(placement.asset)
                    if not isinstance(mesh, unreal.StaticMesh):
                        raise RuntimeError(
                            f"{placement.asset!r} is not a StaticMesh "
                            f"({type(mesh).__name__ if mesh else 'missing'})")
                    component = child.static_mesh_component
                    component.set_mobility(unreal.ComponentMobility.MOVABLE)
                    component.set_static_mesh(mesh)
                    rule = attachment_rule(unreal)
                    child.attach_to_actor(anchor, "", rule, rule, rule)
                    child.root_component.set_relative_location_and_rotation(
                        location, quat_to_rotator(unreal, quat), False, False)
                except Exception as exc:  # noqa: BLE001 - keep going, report the count
                    placement_errors.append(
                        f"{name}/asset_{index}: {type(exc).__name__}: {exc}")
                    continue
                placed += 1
            placed_visuals += placed
            report_links.append({
                "name": name,
                "visual_source": source,
                "visuals": placed,
                "has_mesh": placed > 0,
                "actor_label": anchor.get_actor_label(),
            })
            continue

        if source == SOURCE_PLACEHOLDER:
            report_links.append({
                "name": name,
                "visual_source": source,
                "visuals": 0,
                "has_mesh": False,
                "actor_label": anchor.get_actor_label(),
            })
            continue

        visuals = link["visuals"]
        if args.limit_visuals:
            visuals = visuals[: args.limit_visuals]
        if args.instanced_visuals:
            try:
                placed = add_instanced_visuals(
                    unreal, anchor, visuals, imported, ue_transform,
                    quat_to_rotator, placement_errors, name)
            except Exception as exc:  # noqa: BLE001
                placement_errors.append(f"{name}: {type(exc).__name__}: {exc}")
                placed = 0
            if placed:
                anchor.tags = [f"MuJoCoBody={name}", f"RobotLink={name}",
                               f"RMSimRobot={robot}", f"RMSimLink={name}"]
            placed_visuals += placed
            report_links.append({
                "name": name,
                "visuals": placed,
                "has_mesh": placed > 0,
                "instanced_groups": len(group_visuals_by_mesh(visuals)),
                "actor_label": anchor.get_actor_label(),
            })
            continue
        placed = 0
        for visual in visuals:
            mesh = imported.get(visual["mesh_path"])
            if mesh is None:
                continue
            try:
                location, quat = ue_transform(visual["origin"]["xyz"], visual["origin"]["rpy"])
                child = subsystem.spawn_actor_from_class(
                    unreal.StaticMeshActor, unreal.Vector(0, 0, 0))
                child.set_actor_label(f"{name}_{visual['name']}")
                # Only the link anchor carries ``MuJoCoBody=``: the binder resolves
                # an anchor by that tag and raises on an ambiguous match.
                child.tags = [f"RMSimRobot={robot}", f"RMSimLink={name}"]
                component = child.static_mesh_component
                component.set_mobility(unreal.ComponentMobility.MOVABLE)
                component.set_static_mesh(mesh)
                rule = attachment_rule(unreal)
                child.attach_to_actor(anchor, "", rule, rule, rule)
                child.root_component.set_relative_location_and_rotation(
                    location, quat_to_rotator(unreal, quat), False, False)
            except Exception as exc:  # noqa: BLE001 - keep going, report the count
                placement_errors.append(f"{name}/{visual['name']}: {type(exc).__name__}: {exc}")
                continue
            placed += 1
        placed_visuals += placed
        report_links.append({
            "name": name,
            "visuals": placed,
            "has_mesh": placed > 0,
            "actor_label": anchor.get_actor_label(),
        })

    saved_level = False
    if not args.no_save:
        if tasks:
            unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
        saved_level = unreal.EditorLoadingAndSavingUtils.save_current_level()

    report = {
        "robot": robot,
        "level": args.level,
        "package": package,
        "unique_meshes": len(unique_paths),
        "imported_meshes": len(unique_paths) - len(missing),
        "missing_meshes": missing,
        "removed_previous_actors": removed,
        "links": report_links,
        "placed_visuals": placed_visuals,
        "geometry_check": geometry_check,
        "placement_errors": placement_errors[:20],
        "placement_error_count": len(placement_errors),
        "saved_level": saved_level,
        "skipped_links": sorted(skip_links),
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n",
                           encoding="utf-8")
    print("RMSIM_IMPORT " + json.dumps(report, ensure_ascii=False, default=str), flush=True)
    return 0


def group_visuals_by_mesh(visuals):
    """Group a link's visuals by mesh path, preserving first-seen order.

    Every visual of a link is rigid with respect to its link anchor, so all the
    placements of one mesh can live in a single instanced component instead of
    one actor each.  This is the difference between 955 and 248 transformed
    scene components; docs/render_budget.md section 17 measures the per-frame
    pose cost at roughly 12 us per *transformed component*, not per triangle, so
    grouping is what makes the pose loop cheap.  Unlike baking a merged mesh per
    link it duplicates no geometry -- the four wheel links reference 128 visuals
    that come from only 10 files each.
    """
    groups = {}
    for visual in visuals:
        groups.setdefault(visual["mesh_path"], []).append(visual)
    return list(groups.items())


def add_instanced_visuals(unreal, anchor, visuals, imported, ue_transform,
                          quat_to_rotator, errors, label_prefix):
    """Add one instanced static mesh component per (link, mesh) group to ``anchor``.

    Returns the number of instances placed.  ``SubobjectDataSubsystem`` is the
    only script-exposed way to create a component: ``AddComponentByClass`` is
    ``ScriptNoExport`` and ``RegisterComponent`` is not a ``UFUNCTION``.  The
    component goes on the link anchor, so it inherits the anchor's transform and
    moves with the link without an actor per visual.
    """
    # USubobjectDataSubsystem is a UEngineSubsystem (not an editor subsystem),
    # so it has to come from get_engine_subsystem().
    subsystem = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    handles = subsystem.k2_gather_subobject_data_for_instance(context=anchor)
    if not handles:
        raise RuntimeError("anchor has no subobject handle")
    placed = 0
    for mesh_path, group in group_visuals_by_mesh(visuals):
        mesh = imported.get(mesh_path)
        if mesh is None:
            continue
        try:
            params = unreal.AddNewSubobjectParams(
                parent_handle=handles[0],
                new_class=unreal.HierarchicalInstancedStaticMeshComponent,
                blueprint_context=None)
            # UE's Python bindings promote the ``FText& FailReason`` out
            # parameter to a second return value, so this comes back as
            # ``(handle, fail_reason)`` in some builds and a bare handle in
            # others.  Take the first element in either case.
            handle = subsystem.add_new_subobject(params=params)
            if isinstance(handle, (tuple, list)):
                handle = handle[0]
            # ``K2_FindSubobjectDataFromHandle`` is ``(bool, FSubobjectData&)``
            # in C++, which reaches Python as a tuple; pick the non-bool member.
            found = subsystem.k2_find_subobject_data_from_handle(handle=handle)
            if isinstance(found, (tuple, list)):
                found = next((item for item in found if not isinstance(item, bool)), found[-1])
            component = unreal.SubobjectDataBlueprintFunctionLibrary.get_object(data=found)
            if component is None:
                raise RuntimeError(
                    f"no component for handle: {type(found).__name__} {found!r:.120}")
            component.set_mobility(unreal.ComponentMobility.MOVABLE)
            component.set_static_mesh(mesh)
            component.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
            transforms = []
            for visual in group:
                location, quat = ue_transform(visual["origin"]["xyz"], visual["origin"]["rpy"])
                # ``ue_transform`` already returns binding-native UE types.
                transforms.append(unreal.Transform(
                    location=location,
                    rotation=quat_to_rotator(unreal, quat)))
            # Positional: this build's binding names the parameters without the
            # ``b_`` prefix (``should_return_indices`` ...), so keywords break.
            component.add_instances(transforms, False, False, False)
        except Exception as exc:  # noqa: BLE001 - keep going, report the count
            errors.append(f"{label_prefix}/{mesh_path.rsplit('/', 1)[-1]}: "
                          f"{type(exc).__name__}: {exc}")
            continue
        placed += len(group)
    return placed

if __name__ == "__main__":
    raise SystemExit(main())
