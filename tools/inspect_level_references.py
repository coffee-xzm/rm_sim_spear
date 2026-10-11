"""Report which mesh assets a UE level actually places, and which are orphaned.

Run with the editor commandlet:

    UnrealEditor-Cmd <project>.uproject -run=pythonscript \
        -script=tools/inspect_level_references.py -unattended -nop4 -nosplash

This exists because a *folder* survey answers the wrong question once the level
has been through Merge Actors: merging bakes N source meshes into one mesh asset
and removes the source *actors* from the level, but it does **not** delete the
source *assets*.  So a Content folder can be 95% dead weight while the level runs
on a handful of ``SM_MERGED_*`` meshes.  ``tools/inspect_arena_assets.py``
measures the folder; this measures the level and subtracts.

Two facts that are easy to get wrong and are reported separately:

* **Merge Actors does not reduce triangles.**  It reduces actor and draw-call
  count.  The placed-triangle total is invariant under it, so this tool reports
  the triangles actually placed (mesh triangles x instance count) rather than
  asset-file sizes.
* A mesh can be referenced by an actor that is itself only an unreferenced asset.
  This only walks *level* actors, which is the set that renders.

Arguments come from ``RMSIM_LEVEL_*`` environment variables as well as the
command line, because ``-script=`` accepts only one token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import unreal  # noqa: E402 - must follow the repo-root sys.path insert


DEFAULT_LEVEL = "/Game/creo_stp"
DEFAULT_SURVEY = "/Game/RMUC2026/Geometries"
DEFAULT_REPORT = "/tmp/rm_sim_spear_level_references.json"


def _mesh_path(mesh) -> str | None:
    """``/Game/...`` package of a mesh object, or ``None`` when unset."""
    if mesh is None:
        return None
    try:
        return str(mesh.get_path_name())
    except Exception:  # noqa: BLE001 - a cooked/stripped object can lack a path
        return None


def _package_of(mesh_path: str) -> str:
    """Strip the trailing ``.ObjectName`` so this matches AssetRegistry paths."""
    return mesh_path.split(".", 1)[0]


def _triangles_via_registry(registry, package: str) -> int | None:
    """Triangle count straight off the AssetRegistry, so nothing gets loaded."""
    try:
        data = registry.get_asset_by_object_path(package)
    except Exception:  # noqa: BLE001 - missing package is an answer, not a crash
        return None
    if data is None:
        return None
    try:
        raw = data.get_tag_value("Triangles")
    except Exception:  # noqa: BLE001 - the binding raises on an unknown tag
        return None
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _instances(component) -> int:
    """Instance count for an ISM/HISM; 1 for a plain StaticMeshComponent."""
    try:
        count = component.get_instance_count()
    except Exception:  # noqa: BLE001 - plain components have no instances
        return 1
    try:
        count = int(count)
    except (TypeError, ValueError):
        return 1
    return count if count > 0 else 0


def _collect(actor) -> list[dict]:
    """Every static/skeletal mesh this actor places, with instance counts."""
    found = []
    for component in actor.get_components_by_class(unreal.StaticMeshComponent):
        path = _mesh_path(component.static_mesh)
        if path is None:
            continue
        found.append({"kind": "static", "mesh": path, "instances": _instances(component)})
    for component in actor.get_components_by_class(unreal.SkeletalMeshComponent):
        mesh = None
        try:
            mesh = component.skeletal_mesh
        except Exception:  # noqa: BLE001 - component may expose it differently
            mesh = None
        path = _mesh_path(mesh)
        if path is None:
            continue
        found.append({"kind": "skeletal", "mesh": path, "instances": 1})
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--level", default=os.environ.get("RMSIM_LEVEL_LEVEL", DEFAULT_LEVEL))
    parser.add_argument("--survey", default=os.environ.get("RMSIM_LEVEL_SURVEY", DEFAULT_SURVEY),
                        help="Content folder to report orphaned assets for ('' to skip)")
    parser.add_argument("--report", type=Path,
                        default=Path(os.environ.get("RMSIM_LEVEL_REPORT", DEFAULT_REPORT)))
    parser.add_argument("--top", type=int, default=int(os.environ.get("RMSIM_LEVEL_TOP", "25")))
    args = parser.parse_args()

    if not unreal.EditorLoadingAndSavingUtils.load_map(args.level):
        raise RuntimeError(f"failed to load level: {args.level}")

    subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = subsystem.get_all_level_actors()

    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    # Cache triangle counts: the merged meshes repeat across many actors.
    tri_cache: dict[str, int | None] = {}
    per_mesh: Counter = Counter()
    refs: list[dict] = []
    actor_class: Counter = Counter()
    failures: list[str] = []

    for actor in actors:
        label = actor.get_actor_label()
        actor_class[str(actor.get_class().get_name())] += 1
        try:
            placed = _collect(actor)
        except Exception as exc:  # noqa: BLE001 - keep going, report the count
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
            continue
        for item in placed:
            package = _package_of(item["mesh"])
            per_mesh[package] += item["instances"]
            refs.append({"actor": label, "actor_class": str(actor.get_class().get_name()),
                         "package": package, **item})

    for package in per_mesh:
        tri_cache[package] = _triangles_via_registry(registry, package)

    # "Placed triangles" = mesh triangles x instances.  Merge Actors leaves this
    # number unchanged; only actor count moves.
    placed_triangles = 0
    unknown_triangles = []
    for package, count in per_mesh.items():
        tris = tri_cache.get(package)
        if tris is None:
            unknown_triangles.append(package)
            continue
        placed_triangles += tris * count

    orphaned: dict | None = None
    if args.survey:
        try:
            candidates = sorted(unreal.EditorAssetLibrary.list_assets(args.survey, recursive=True))
        except Exception as exc:  # noqa: BLE001 - a bad path must not sink the report
            candidates, failures = [], failures + [f"survey {args.survey}: {exc}"]
        used = set(per_mesh)
        unused = [p for p in candidates if p not in used]
        registry_sizes = {}
        for package in unused:
            try:
                data = registry.get_asset_by_object_path(package)
                size = data.get_tag_value("EstTotalCompressedSize") if data else None
            except Exception:  # noqa: BLE001 - size is best-effort
                size = None
            try:
                registry_sizes[package] = int(size) if size not in (None, "") else None
            except (TypeError, ValueError):
                registry_sizes[package] = None
        known = [registry_sizes[p] for p in unused if registry_sizes[p] is not None]
        orphaned = {
            "survey": args.survey,
            "candidates": len(candidates),
            "referenced_by_level": sum(1 for p in candidates if p in used),
            "not_referenced": len(unused),
            "not_referenced_est_compressed_bytes": sum(known),
            "not_referenced_with_known_size": len(known),
            "examples": unused[:args.top],
        }

    report = {
        "level": args.level,
        "actor_count": len(actors),
        "actor_class_histogram": dict(actor_class.most_common()),
        "distinct_meshes_placed": len(per_mesh),
        "total_mesh_instances": sum(per_mesh.values()),
        "placed_triangles": placed_triangles,
        "meshes_without_triangle_tag": sorted(unknown_triangles),
        "heaviest_by_placed_triangles": [
            {"package": p, "instances": c, "triangles": tri_cache.get(p),
             "placed_triangles": (tri_cache.get(p) or 0) * c}
            for p, c in sorted(
                per_mesh.items(),
                key=lambda kv: (tri_cache.get(kv[0]) or 0) * kv[1],
                reverse=True)[:args.top]
        ],
        "orphaned_assets": orphaned,
        "placements": refs,
        "component_read_failures": failures,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")

    print(json.dumps({
        "event": "level_references",
        "level": args.level,
        "report": str(args.report),
        "actor_count": len(actors),
        "distinct_meshes_placed": len(per_mesh),
        "total_mesh_instances": sum(per_mesh.values()),
        "placed_triangles": placed_triangles,
        "orphaned": orphaned,
    }, ensure_ascii=False))
    unreal.log(f"rm_sim_spear: {len(actors)} actors, {len(per_mesh)} distinct meshes -> "
               f"{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
